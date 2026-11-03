"""
rag_eval - RAGAS 离线评测（参考 rag-pro/assessment/rag_as.py）

用途：对一批 (question, answer, contexts, reference) 跑 RAGAS 指标，量化检索/生成质量。
与线上运行时解耦——本项目线上 LLM 结果审计仍是 no-op（router._audit_llm_result），
评测只在独立环境批量跑，依赖（ragas / langchain-community）不进运行时 requirements.txt。

数据来源（二选一，CLI 切换）：
  1) --file <path>     读取人工标注 JSON（rag-pro 风格，每条含 reference）
  2) --from-db         从业务库捞真实检索数据：
       - question/answer 来自 rag_turn（user/assistant 两行按 turn_id 配对，tenant 隔离）
       - retrieved_contexts 来自 rag_turn.chunks_json 的 chunk_id → 回查 rag_chunk.content
         （chunks_json 若存了全文 text 则直接用，免 join；详见 docs/会话记录持久化设计方案 §9.2）
       - reference 来自 rag_eval_label（人工/外部标准答案，LEFT JOIN；缺标则只跑两项指标）

指标：
  - Faithfulness    忠实度：答案是否忠于检索上下文（查幻觉）—— 不需要 reference
  - AnswerRelevancy 答案相关性：是否切题 —— 不需要 reference
  - ContextPrecision 上下文精确度：召回里有多少真有用（查噪声）—— 需要 reference
  - ContextRecall   上下文召回率：召回是否覆盖参考答案关键信息（查漏）—— 需要 reference

依赖：pip install -r offline/requirements.ragas.txt
"""
import argparse
import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel

from common.config import config
from common.logger import logger, setup_logging

load_dotenv(dotenv_path=config.BASE_DIR / '.env', override=True)


def _build_clients() -> tuple[AsyncOpenAI, AsyncOpenAI]:
    """构造评测用的 LLM 与 embedding 异步客户端（均 OpenAI 兼容协议）。"""
    llm_client = AsyncOpenAI(
        base_url=config.RAGAS_LLM_BASE_URL, api_key=config.RAGAS_LLM_API_KEY,
    )
    embed_client = AsyncOpenAI(
        base_url=config.RAGAS_EMBED_BASE_URL, api_key='ollama',  # 本地 ollama 不校验 key
    )
    return llm_client, embed_client


async def _build_ragas_assets():
    """惰性导入 ragas 并构造 llm / embeddings 实例（避免无依赖时导入即报错）。"""
    from ragas.embeddings.base import embedding_factory
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (  # noqa: WPS433
        AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness,
    )
    llm_client, embed_client = _build_clients()
    llm = llm_factory(model=config.RAGAS_LLM_MODEL, client=llm_client)
    embeddings = embedding_factory(
        provider='openai', model=config.RAGAS_EMBED_MODEL, client=embed_client, interface='modern',
    )
    metrics = {
        'faithfulness': Faithfulness(llm=llm),
        'answer_relevancy': AnswerRelevancy(llm=llm, embeddings=embeddings),
        'context_precision': ContextPrecision(llm=llm),
        'context_recall': ContextRecall(llm=llm),
    }
    return metrics


class EvalResult(BaseModel):
    """单条样本评测得分；ContextPrecision/ContextRecall 在缺 reference 时为 None。"""

    faithfulness: float
    answer_relevancy: float
    context_precision: float | None = None
    context_recall: float | None = None


async def run_evaluation(row: dict[str, Any]) -> EvalResult:
    """对单条样本计算指标（参考 rag-pro 的 experiment 算子写法，指标并发打分）。

    row 需含 user_input / response / retrieved_contexts；reference 可选。
    缺 reference 时只跑 Faithfulness + AnswerRelevancy（另两项需 ground truth）。
    """
    metrics = await _build_ragas_assets()
    user_input = row['user_input']
    response = row['response']
    contexts = row['retrieved_contexts']
    reference = row.get('reference')
    faith = await metrics['faithfulness'].ascore(
        user_input=user_input, response=response, retrieved_contexts=contexts,
    )
    relev = await metrics['answer_relevancy'].ascore(user_input=user_input, response=response)
    result = {'faithfulness': faith.value, 'answer_relevancy': relev.value}
    if reference:
        prec = await metrics['context_precision'].ascore(
            user_input=user_input, reference=reference, retrieved_contexts=contexts,
        )
        recal = await metrics['context_recall'].ascore(
            user_input=user_input, retrieved_contexts=contexts, reference=reference,
        )
        result['context_precision'] = prec.value
        result['context_recall'] = recal.value
    return EvalResult(**result)


def load_dataset(path: str) -> list[dict[str, Any]]:
    """从 JSON 文件加载评测样本（数组或 {"samples": [...]} 两种结构都支持）。"""
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get('samples', [])
    if not isinstance(data, list):
        raise ValueError(f'数据集格式错误，期望数组: {path}')
    required = {'user_input', 'response', 'retrieved_contexts', 'reference'}
    for i, item in enumerate(data):
        missing = required - item.keys()
        if missing:
            raise ValueError(f'第 {i} 条样本缺字段: {missing}')
    return data


async def load_from_db(
    tenant_id: int,
    limit: int = 200,
    eval_flag_only: bool = False,
    since: str | None = None,
    until: str | None = None,
    strategy: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """从业务库捞真实检索数据，组装成 RAGAS 样本。

    返回每条含 user_input / response / retrieved_contexts；reference 可能为空（缺标）。
    """
    # 惰性导入，避免仅跑 JSON 评测时也拉起 DB / ORM 模块
    from sqlalchemy import and_, select
    from sqlalchemy.orm import aliased

    from store.orm_models.knowledge import Chunk
    from store.orm_models.session import RagEvalLabel, RagTurn
    from store.postgres_store import get_sessionmaker

    A = aliased(RagTurn, name='a')  # assistant 行
    U = aliased(RagTurn, name='u')  # user 行

    stmt = (
        select(
            U.content.label('question'), A.turn_id, A.content.label('answer'),
            A.chunks_json, A.strategy, A.created_at, RagEvalLabel.reference,
        )
        .select_from(A)
        .join(U, and_(U.turn_id == A.turn_id, U.role == 'user'))
        .outerjoin(RagEvalLabel, RagEvalLabel.turn_id == A.turn_id)
        .where(A.role == 'assistant', A.tenant_id == tenant_id)
    )
    if eval_flag_only:
        stmt = stmt.where(A.eval_flag == 1)
    if since:
        stmt = stmt.where(A.created_at >= since)
    if until:
        stmt = stmt.where(A.created_at <= until)
    if strategy:
        stmt = stmt.where(A.strategy == strategy)
    if source:
        stmt = stmt.where(A.source == source)
    stmt = stmt.order_by(A.created_at.desc()).limit(limit)

    factory = get_sessionmaker()
    async with factory() as session:
        rows = (await session.execute(stmt)).all()

    # 先收集需要回查正文的 chunk_id，再批量 join rag_chunk，避免 N+1
    to_resolve: set[int] = set()
    parsed: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for r in rows:
        refs: list[dict[str, Any]] = []
        if r.chunks_json:
            try:
                refs = json.loads(r.chunks_json)
            except (json.JSONDecodeError, TypeError):
                refs = []
        for ref in refs:
            if not ref.get('text'):  # 未存全文才需要回查
                cid = ref.get('chunk_id')
                if isinstance(cid, int):
                    to_resolve.add(cid)
        parsed.append((r, refs))

    content_map: dict[int, str] = {}
    if to_resolve:
        async with factory() as session:
            crows = (await session.execute(
                select(Chunk.chunk_id, Chunk.content).where(
                    Chunk.chunk_id.in_(list(to_resolve)), Chunk.tenant_id == tenant_id,
                )
            )).all()
        content_map = {cid: content for cid, content in crows}

    samples: list[dict[str, Any]] = []
    for r, refs in parsed:
        contexts = _resolve_contexts(refs, content_map)
        samples.append({
            'user_input': r.question or '',
            'response': r.answer or '',
            'retrieved_contexts': contexts,
            'reference': r.reference,  # 可能为 None
        })
    logger.info(f'从库捞出评测样本 {len(samples)} 条 [tenant={tenant_id}]')
    return samples


def _resolve_contexts(refs: list[dict[str, Any]], content_map: dict[int, str]) -> list[str]:
    """把 chunks_json 的引用列表还原成召回正文列表。

    优先用 ref 自带的全文 text（开 QA_PERSIST_CHUNK_FULLTEXT 时）；否则按 chunk_id
    回查 content_map（来自 rag_chunk）。空文本丢弃，保持原顺序。
    """
    contexts: list[str] = []
    for ref in refs:
        ctx = ref.get('text') or content_map.get(ref.get('chunk_id')) or ''
        if ctx:
            contexts.append(ctx)
    return contexts


async def save_eval_label(turn_id: str, reference: str) -> None:
    """写入/更新一条评测标注（ground truth），供 load_from_db 的 reference 字段使用。"""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from store.orm_models.session import RagEvalLabel
    from store.postgres_store import get_sessionmaker

    factory = get_sessionmaker()
    async with factory() as session:
        stmt = pg_insert(RagEvalLabel).values(
            turn_id=turn_id, reference=reference,
        ).on_conflict_do_update(index_elements=['turn_id'], set_={'reference': reference})
        await session.execute(stmt)
        await session.commit()
    logger.info(f'已写入评测标注 [turn_id={turn_id}]')


async def _run_samples(samples: list[dict[str, Any]]) -> None:
    agg: dict[str, list[float]] = {
        'faithfulness': [], 'answer_relevancy': [],
        'context_precision': [], 'context_recall': [],
    }
    labeled = 0
    for idx, row in enumerate(samples):
        result = await run_evaluation(row)
        if row.get('reference'):
            labeled += 1
        for key in agg:
            val = getattr(result, key)
            if val is not None:
                agg[key].append(val)
        print(f'[{idx}] {result.model_dump_json(indent=2)}')
    summary = {key: (sum(vals) / len(vals) if vals else None) for key, vals in agg.items()}
    print('--- 汇总（均值，None=该指标无可用样本）---')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'样本 {len(samples)} 条，其中带 reference 可跑四项指标的 {labeled} 条')


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='RAGAS 离线评测（数据来自 JSON 或业务库）')
    parser.add_argument('--file', help='JSON 评测文件路径（含 reference 的人工标注集）')
    parser.add_argument('--from-db', action='store_true', help='从业务库捞真实检索数据')
    parser.add_argument('--tenant', type=int, default=0, help='租户ID（--from-db 必填）')
    parser.add_argument('--limit', type=int, default=200, help='捞取样本上限')
    parser.add_argument('--eval-flag', action='store_true', help='只捞 eval_flag=1 的采样轮次')
    parser.add_argument('--since', help='起始时间 YYYY-MM-DD HH:MM:SS')
    parser.add_argument('--until', help='结束时间 YYYY-MM-DD HH:MM:SS')
    parser.add_argument('--strategy', help='按检索策略过滤（direct|hyde|subquery|step_back）')
    parser.add_argument('--source', help='按产出来源过滤（cache|faq|rag|empty）')
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    if args.from_db:
        if args.tenant == 0:
            raise ValueError('--from-db 需要 --tenant')
        samples = await load_from_db(
            tenant_id=args.tenant, limit=args.limit, eval_flag_only=args.eval_flag,
            since=args.since, until=args.until, strategy=args.strategy, source=args.source,
        )
    elif args.file:
        samples = load_dataset(args.file)
    else:
        samples = load_dataset(str(config.RES_DIR / 'rag_evaluate_data.json'))
    if not samples:
        print('无样本，退出')
        return
    await _run_samples(samples)


if __name__ == '__main__':
    setup_logging()
    _args = _build_arg_parser().parse_args()
    asyncio.run(_main_async(_args))
