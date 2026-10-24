"""
router - 问答路由编排

顺序：答案缓存（先行，命中即返回）→ 意图识别 → FAQ 命中（精确/BM25 相似匹配则直接取标准答案）
→ 意图分流 + 检索增强 + 生成。
FAQ 命中时不调用大模型，未命中再走 RAG。FAQ 匹配见 store.faq_cache（先字面精确，再 BM25 相似+阈值）。

意图分流：
- 通用知识 → 空上下文交由大模型用自身知识直答（不检索、不因无库命中而拒绝）；
- 专业咨询 → 经 LLM 选检索增强策略（HyDE/子查询/回溯/直接）检索，检索无命中才回复"未找到"。

检索条数由 config.RECALL_K（召回候选）/ config.RERANK_TOP_K（精排保留）统一控制，不按请求传 top_k。

预留占位能力（调用点已打通，逻辑待落地，详见 docs/ 下对应设计方案）：
  - 上下文压缩（待设计，见 docs/上下文压缩设计方案.md）：检索片段超长时压缩后再生成
  - 会话记录持久化（待设计，见 docs/会话记录持久化设计方案.md）
  - LLM 结果审计（后续接入 RAGAS 评测管道，不在业务库持久化）：审计/质量评估交由 RAGAS
（答案缓存前置已落地，不在此列。）
"""
import asyncio
import random
import time
from typing import Any

from common.config import config
from common.logger import logger
from app.app_state import app_state, get_tenant_id
from core.qa_router.compression import CompressResult, compress_context
from core.session.session_service import SessionService
from core.rag_retriever.retriever import RetrievedChunk, Retriever
from core.rag_retriever.strategy_selector import RetrievalStrategy, StrategySelector
from core.rag_retriever.query_augmenter import QueryAugmenter
from core.rag_generator.generator import Generator
from core.intent import classify_query
from store.faq_cache import get_faq_answer
from store.redis_store import get_answer, set_answer


class QARouter:
    """问答入口：先做 FAQ 匹配，未命中再走 RAG（含意图分流与检索增强）。"""

    def __init__(
        self,
        retriever: Retriever | None = None,
        generator: Generator | None = None,
        strategy_selector: StrategySelector | None = None,
        augmenter: QueryAugmenter | None = None,
    ) -> None:
        self.retriever = retriever or Retriever()
        self.generator = generator or Generator()
        self.strategy_selector = strategy_selector or StrategySelector()
        self.augmenter = augmenter or QueryAugmenter(self.retriever)
        # 落库走 core/session 的服务与 DAO，router 不直接写 SQL
        self.session_service = SessionService()
        self.session_store = self.session_service.store
        self._turn_tasks: set[asyncio.Task] = set()   # 持有异步落库 task 引用，避免被 GC

    async def answer(
        self,
        query: str,
        tenant_id: int,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """问答主流程，返回 {answer, source, chunks, session_id, intent}。

        source 取值：cache / faq / rag / empty。
        """
        if not query or not query.strip():
            return {'answer': '', 'source': 'empty', 'chunks': [], 'session_id': session_id, 'intent': '通用知识'}

        turn_id = self.session_store.new_turn_id()
        started = time.time()

        # 0) 答案缓存前置：先查缓存，命中直接返回，跳过意图识别与检索/生成
        cached = await get_answer(tenant_id, query)
        if cached:
            logger.debug(f'答案缓存命中 [tenant={tenant_id}]')
            await self._persist_session(query, cached, session_id, '通用知识', user_id,
                                        meta={'turn_id': turn_id, 'source': 'cache',
                                              'tenant_id': tenant_id,
                                              'latency_ms': int((time.time() - started) * 1000)})
            return {'answer': cached, 'source': 'cache', 'chunks': [], 'session_id': session_id, 'intent': '通用知识'}

        # 1) 意图识别（轻量 BERT，异步不阻塞事件循环）；结果用于分流与可观测
        intent = await classify_query(query)

        # 2) FAQ 优先（字面精确 + BM25 相似，命中即取标准答案，不调用大模型）
        answer, faq_id = await get_faq_answer(tenant_id, query)
        if answer:
            await set_answer(tenant_id, query, answer)
            logger.debug(f'FAQ 命中 [tenant={tenant_id}, faq_id={faq_id}]')
            await self._persist_session(query, answer, session_id, intent, user_id,
                                        meta={'turn_id': turn_id, 'source': 'faq',
                                              'tenant_id': tenant_id,
                                              'latency_ms': int((time.time() - started) * 1000)})
            return {'answer': answer, 'source': 'faq', 'chunks': [], 'session_id': session_id, 'intent': intent}

        # 3) 意图分流 + 检索增强
        strategy: RetrievalStrategy | None = None
        if intent == '通用知识':
            # 通用知识：空上下文交由大模型用自身知识直答（不检索、不因无库命中而拒绝）
            rag_hits: list[RetrievedChunk] = []
        else:
            # 专业咨询：LLM 选检索增强策略；检索无命中才回复"未找到"
            strategy = await self.strategy_selector.select_strategy(query)
            rag_hits = await self.augmenter.augment_and_retrieve(
                query, strategy, tenant_id=tenant_id)
            if not rag_hits:
                await self._persist_session(query, '抱歉，未找到答案！', session_id, intent, user_id,
                                            meta={'turn_id': turn_id, 'source': 'empty',
                                                  'tenant_id': tenant_id, 'strategy': strategy.value if strategy else None,
                                                  'latency_ms': int((time.time() - started) * 1000)})
                return {'answer': '抱歉，未找到答案！', 'source': 'empty', 'chunks': [], 'session_id': session_id, 'intent': intent}

        # 4) 上下文压缩（按大小配置，兜底手段；失败回退原样）
        compress_result = await self._compress_context(query, rag_hits, session_id)
        rag_hits = compress_result.chunks

        # 5) 大模型生成
        answer = await self.generator.generate(query=query, chunks=rag_hits, session_id=session_id, user_id=user_id)
        # 6) 审计（后续接入 RAGAS 评测管道）+ 问答明细落库
        await self._audit_llm_result(query, answer, rag_hits, None, user_id)
        await self._persist_session(query, answer, session_id, intent, user_id, meta={
            'turn_id': turn_id, 'source': 'rag', 'tenant_id': tenant_id,
            'strategy': strategy.value if strategy else None,
            'model': config.LLM_MODEL_NAME, 'chunks': rag_hits,
            'compress_method': compress_result.method,
            'compress_dropped': compress_result.dropped,
            'latency_ms': int((time.time() - started) * 1000),
        })
        await set_answer(tenant_id, query, answer)
        return {
            'answer': answer,
            'source': 'rag',
            'intent': intent,
            'chunks': [{'content': c.content, 'doc_name': c.doc_name, 'page': c.page,
                        'score': c.rerank_score} for c in rag_hits],
            # 透出压缩方式与丢弃片段数：回答可能未覆盖全部检索结果，不能静默丢信息
            'compress': compress_result.to_dict(),
            'session_id': session_id,
        }

    async def answer_stream(
        self,
        query: str,
        tenant_id: int,
        session_id: str | None = None,
        user_id: str | None = None,
    ):
        """流式问答：与 answer 同链路，但逐段 yield (event, data) 供 SSE 输出。

        事件协议：
        - ("meta", {source, intent, session_id[, chunks]})：阶段元信息（rag 时带召回片段）
        - ("answer", 文本)：缓存/FAQ/空 的整段答案
        - ("token", 文本)：RAG 逐 token 输出
        - ("done", {})：结束
        """
        if not query or not query.strip():
            yield ("meta", {"source": "empty", "intent": "通用知识", "session_id": session_id})
            yield ("answer", "")
            yield ("done", {})
            return

        turn_id = self.session_store.new_turn_id()
        started = time.time()

        # 0) 答案缓存前置
        cached = await get_answer(tenant_id, query)
        if cached:
            logger.debug(f'答案缓存命中 [tenant={tenant_id}]')
            await self._persist_session(query, cached, session_id, '通用知识', user_id,
                                        meta={'turn_id': turn_id, 'source': 'cache',
                                              'tenant_id': tenant_id,
                                              'latency_ms': int((time.time() - started) * 1000)})
            yield ("meta", {"source": "cache", "intent": "通用知识", "session_id": session_id})
            yield ("answer", cached)
            yield ("done", {})
            return

        # 1) 意图识别
        intent = await classify_query(query)

        # 2) FAQ 优先
        answer, faq_id = await get_faq_answer(tenant_id, query)
        if answer:
            await set_answer(tenant_id, query, answer)
            logger.debug(f'FAQ 命中 [tenant={tenant_id}, faq_id={faq_id}]')
            await self._persist_session(query, answer, session_id, intent, user_id,
                                        meta={'turn_id': turn_id, 'source': 'faq',
                                              'tenant_id': tenant_id,
                                              'latency_ms': int((time.time() - started) * 1000)})
            yield ("meta", {"source": "faq", "intent": intent, "session_id": session_id})
            yield ("answer", answer)
            yield ("done", {})
            return

        # 3) 意图分流 + 检索增强
        strategy: RetrievalStrategy | None = None
        if intent == '通用知识':
            rag_hits: list[RetrievedChunk] = []  # 空上下文，交由大模型用自身知识直答
        else:
            strategy = await self.strategy_selector.select_strategy(query)
            rag_hits = await self.augmenter.augment_and_retrieve(
                query, strategy, tenant_id=tenant_id)
            if not rag_hits:
                # 专业咨询检索无命中：不生成，直接回复"未找到"
                await self._persist_session(query, '抱歉，未找到答案！', session_id, intent, user_id,
                                            meta={'turn_id': turn_id, 'source': 'empty',
                                                  'tenant_id': tenant_id,
                                                  'strategy': strategy.value if strategy else None,
                                                  'latency_ms': int((time.time() - started) * 1000)})
                yield ("meta", {"source": "empty", "intent": intent, "session_id": session_id})
                yield ("answer", "抱歉，未找到答案！")
                yield ("done", {})
                return

        # 4) 上下文压缩（放在 meta 之前，保证前端拿到的 chunks 与实际送给模型的一致）
        compress_result = await self._compress_context(query, rag_hits, session_id)
        rag_hits = compress_result.chunks

        chunks_meta = [
            {'content': c.content, 'doc_name': c.doc_name, 'page': c.page, 'score': c.rerank_score}
            for c in rag_hits
        ]
        yield ("meta", {"source": "rag", "intent": intent, "session_id": session_id,
                        "chunks": chunks_meta, "compress": compress_result.to_dict()})

        # 5) 流式生成：逐 token 产出，并累计完整答案用于缓存
        full_parts: list[str] = []
        async for piece in self.generator.astream(
            query=query, chunks=rag_hits, session_id=session_id, user_id=user_id,
        ):
            full_parts.append(piece)
            yield ("token", piece)

        # 6) 审计 + 问答明细落库 + 答案缓存
        full_answer = "".join(full_parts)
        await self._audit_llm_result(query, full_answer, rag_hits, strategy, user_id)
        await self._persist_session(query, full_answer, session_id, intent, user_id, meta={
            'turn_id': turn_id, 'source': 'rag', 'tenant_id': tenant_id,
            'strategy': strategy.value if strategy else None,
            'model': config.LLM_MODEL_NAME, 'chunks': rag_hits,
            'compress_method': compress_result.method,
            'compress_dropped': compress_result.dropped,
            'latency_ms': int((time.time() - started) * 1000),
        })
        await set_answer(tenant_id, query, full_answer)
        yield ("done", {})

    # ------------------------------------------------------------------
    # 以下为预留占位能力：尚未实现，仅打通调用点，后续按需填充落地逻辑。
    # ------------------------------------------------------------------

    async def _compress_context(
        self, query: str, chunks: list[RetrievedChunk], session_id: str | None
    ) -> CompressResult:
        """上下文压缩：检索片段累计超长时按大小截取/精选/摘要（设计方案见 docs/上下文压缩设计方案.md）。

        压缩是兜底手段，任何异常都回退原样，不阻断生成。
        """
        try:
            return await compress_context(query, chunks, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'上下文压缩失败，回退原样 [session={session_id}]: {e}')
            return CompressResult(
                chunks=chunks, method='none',
                chars_before=sum(len(c.content or '') for c in chunks),
                chars_after=sum(len(c.content or '') for c in chunks),
                kept_indices=list(range(len(chunks))), dropped=0,
            )

    def _spawn_persist(self, coro) -> None:
        """把落库协程丢到后台：持有 task 引用防 GC，并发超上限直接丢弃。

        明细是审计/统计数据，丢一轮比拖慢主链路划算，所以超限时不排队而是丢弃并告警。
        """
        if len(self._turn_tasks) >= config.QA_PERSIST_MAX_CONCURRENCY:
            coro.close()
            logger.warning('问答明细落库并发超限，丢弃本轮明细')
            return
        task = asyncio.create_task(coro)
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _persist_session(
        self, query: str, answer: str, session_id: str | None, intent: str,
        user_id: str | None = None, meta: dict[str, Any] | None = None,
    ) -> None:
        """问答明细落库：会话原文写 Redis（结构化）+ 明细写 MySQL rag_turn。

        meta 携带本轮产出侧信息（turn_id / source / tenant_id / strategy / model /
        chunks / 压缩方式与丢弃数 / 耗时）。异步执行、失败只告警，绝不抛到主链路。
        """
        if not config.QA_PERSIST_ENABLED or not session_id or not user_id:
            return
        meta = dict(meta or {})
        turn_id = meta.pop('turn_id', None) or self.session_store.new_turn_id()
        tenant_id = meta.pop('tenant_id', None) or get_tenant_id() or 0
        # 评测采样在写入时定：异步落库后再回写 eval_flag 会和插入抢，命中不可靠
        eval_flag = 1 if random.random() < config.QA_EVAL_SAMPLE_RATE else 0

        user_item, asst_item, rows = self.session_store.build_turn_payload(
            turn_id=turn_id, user_id=user_id, session_id=session_id, query=query, answer=answer,
            tenant_id=tenant_id, intent=intent, eval_flag=eval_flag, **meta)
        # 会话原文里同样带上明细字段（QA 属性只在 assistant 侧），回看时无需再查 MySQL
        user_extra = {'turn_id': turn_id}
        asst_extra = {k: v for k, v in asst_item.items() if k not in ('role', 'content', 'ts')}

        async def _write() -> None:
            try:
                await self.session_service.append_turn(user_id, session_id, 'user', query, extra=user_extra)
                await self.session_service.append_turn(user_id, session_id, 'assistant', answer, extra=asst_extra)
                await self.session_store.persist_turn_rows(rows)
            except Exception as e:  # noqa: BLE001  落库失败只告警，不影响已给出的回答
                logger.warning(f'问答明细落库失败，跳过 [session={session_id}, turn={turn_id}]: {e}')

        if config.QA_PERSIST_ASYNC:
            self._spawn_persist(_write())
        else:
            await _write()

    async def _audit_llm_result(
        self, query: str, answer: str, chunks: list[RetrievedChunk], strategy: RetrievalStrategy | None,
        user_id: str | None = None,
    ) -> None:
        """LLM 结果审计占位：审计/质量评估交由 RAGAS 评测管道，不在业务库持久化。"""
        # 后续由 RAGAS 评测框架落地：question/answer/context/strategy 作为评测信号采集，
        # 不在此做业务库写入（保留 no-op，避免无依赖时阻断主链路）。
        logger.debug(f'LLM 结果审计交由 RAGAS 评测管道，跳过 [strategy={strategy}, user={user_id}]')
