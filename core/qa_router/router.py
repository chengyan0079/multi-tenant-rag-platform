"""
router - 问答路由编排（M9）

顺序：答案缓存（先行，命中即返回）→ 意图识别 → FAQ 命中（精确/BM25 相似匹配则直接取标准答案）
→ 意图分流 + 检索增强 + 生成。
FAQ 命中时不调用大模型，未命中再走 RAG。FAQ 匹配见 store.faq_cache（先字面精确，再 BM25 相似+阈值）。

意图分流：
- 通用知识 → 空上下文交由大模型用自身知识直答（不检索、不因无库命中而拒绝）；
- 专业咨询 → 经 LLM 选检索增强策略（HyDE/子查询/回溯/直接）检索，检索无命中才回复"未找到"。

检索条数由 config.RECALL_K（召回候选）/ config.RERANK_TOP_K（精排保留）统一控制，不按请求传 top_k。

预留占位能力（尚未实现，仅打通调用点，用 TODO 标记）：
  - 上下文压缩（TODO）：检索片段超长时压缩后再生成
  - 回答打分（TODO）：用户对回答打分并持久化
  - 会话记录持久化（TODO）
  - LLM 结果审计（TODO）
（答案缓存前置已落地，不在此列。）
"""
from typing import Any

from common.config import config
from common.logger import logger
from app.app_state import app_state
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

        # 0) 答案缓存前置：先查缓存，命中直接返回，跳过意图识别与检索/生成
        cached = await get_answer(tenant_id, query)
        if cached:
            logger.debug(f'答案缓存命中 [tenant={tenant_id}]')
            return {'answer': cached, 'source': 'cache', 'chunks': [], 'session_id': session_id, 'intent': '通用知识'}

        # 1) 意图识别（轻量 BERT，异步不阻塞事件循环）；结果用于分流与可观测
        intent = await classify_query(query)

        # 2) FAQ 优先（字面精确 + BM25 相似，命中即取标准答案，不调用大模型）
        answer, faq_id = await get_faq_answer(tenant_id, query)
        if answer:
            await set_answer(tenant_id, query, answer)
            logger.debug(f'FAQ 命中 [tenant={tenant_id}, faq_id={faq_id}]')
            return {'answer': answer, 'source': 'faq', 'chunks': [], 'session_id': session_id, 'intent': intent}

        # 3) 意图分流 + 检索增强
        if intent == '通用知识':
            # 通用知识：空上下文交由大模型用自身知识直答（不检索、不因无库命中而拒绝）
            rag_hits: list[RetrievedChunk] = []
        else:
            # 专业咨询：LLM 选检索增强策略；检索无命中才回复"未找到"
            strategy = await self.strategy_selector.select_strategy(query)
            rag_hits = await self.augmenter.augment_and_retrieve(
                query, strategy, tenant_id=tenant_id)
            if not rag_hits:
                return {'answer': '抱歉，未找到答案！', 'source': 'empty', 'chunks': [], 'session_id': session_id, 'intent': intent}

        # 4) TODO: 上下文压缩占位 - 检索片段超长时压缩后再送生成（未实现，当前原样返回）
        rag_hits = await self._compress_context(query, rag_hits, session_id)

        # 5) 大模型生成
        answer = await self.generator.generate(query=query, chunks=rag_hits, session_id=session_id, user_id=user_id)
        # 6) TODO: 审计 + 会话记录持久化占位（未实现，当前跳过）
        await self._audit_llm_result(query, answer, rag_hits, None, user_id)
        await self._persist_session(query, answer, session_id, intent, user_id)
        await set_answer(tenant_id, query, answer)
        return {
            'answer': answer,
            'source': 'rag',
            'intent': intent,
            'chunks': [{'content': c.content, 'doc_name': c.doc_name, 'page': c.page,
                        'score': c.rerank_score} for c in rag_hits],
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

        # 0) 答案缓存前置
        cached = await get_answer(tenant_id, query)
        if cached:
            logger.debug(f'答案缓存命中 [tenant={tenant_id}]')
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
                yield ("meta", {"source": "empty", "intent": intent, "session_id": session_id})
                yield ("answer", "抱歉，未找到答案！")
                yield ("done", {})
                return

        chunks_meta = [
            {'content': c.content, 'doc_name': c.doc_name, 'page': c.page, 'score': c.rerank_score}
            for c in rag_hits
        ]
        yield ("meta", {"source": "rag", "intent": intent, "session_id": session_id, "chunks": chunks_meta})

        # 4) 上下文压缩占位
        rag_hits = await self._compress_context(query, rag_hits, session_id)

        # 5) 流式生成：逐 token 产出，并累计完整答案用于缓存
        full_parts: list[str] = []
        async for piece in self.generator.astream(
            query=query, chunks=rag_hits, session_id=session_id, user_id=user_id,
        ):
            full_parts.append(piece)
            yield ("token", piece)

        # 6) 审计 + 会话持久化占位 + 答案缓存
        full_answer = "".join(full_parts)
        await self._audit_llm_result(query, full_answer, rag_hits, strategy, user_id)
        await self._persist_session(query, full_answer, session_id, intent, user_id)
        await set_answer(tenant_id, query, full_answer)
        yield ("done", {})

    # ------------------------------------------------------------------
    # 以下为预留占位能力：尚未实现，仅打通调用点，后续按需填充落地逻辑。
    # ------------------------------------------------------------------

    async def _compress_context(
        self, query: str, chunks: list[RetrievedChunk], session_id: str | None
    ) -> list[RetrievedChunk]:
        """上下文压缩占位：检索片段累计超长时，裁剪/压缩后再送生成。待实现。"""
        # TODO: 依据 query 相关性或总长度阈值，对 chunks 做截断/摘要压缩
        return chunks

    async def _persist_session(
        self, query: str, answer: str, session_id: str | None, intent: str, user_id: str | None = None
    ) -> None:
        """会话记录持久化占位：把用户问题/回答写入会话存储（MySQL/Redis）。待实现。"""
        # TODO: 落库会话明细，关联 tenant_id / user_id / session_id / intent
        logger.debug(f'会话记录持久化未实现，跳过 [session={session_id}, user={user_id}]')

    async def _audit_llm_result(
        self, query: str, answer: str, chunks: list[RetrievedChunk], strategy: RetrievalStrategy | None,
        user_id: str | None = None,
    ) -> None:
        """LLM 结果审计占位：记录生成链路审计信息（query/答案/片段/策略）。待实现。"""
        # TODO: 写入审计日志（谁、何时、问什么、答什么、用了哪些片段与策略）
        logger.debug(f'LLM 结果审计未实现，跳过 [strategy={strategy}, user={user_id}]')

    async def record_rating(
        self, session_id: str | None, rating: int, comment: str = ''
    ) -> None:
        """回答打分持久化占位：用户对回答打分（建议 1-5）并可选留言，落库待实现。

        由接入层在用户提交打分时调用。
        """
        # TODO: 持久化评分，关联 session_id 与对应回答
        logger.debug(f'回答打分持久化未实现，跳过 [session={session_id}, rating={rating}]')
