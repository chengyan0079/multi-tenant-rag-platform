"""
query_augmenter - 检索增强 / query 改写

在检索前对 query 做变换，提升召回质量，对应 RAGDemo 的 HyDE / 子查询 / 回溯：
  - DIRECT    直接用原问题检索
  - HYDE      生成假设性文档，用该文档做向量检索
  - SUB_QUERY 拆成多个子问题分别检索后合并去重
  - BACKTRACK 把复杂问题抽象为核心问题再检索
任一增强结果为空时自动回退 DIRECT，保证检索不空。
"""
from typing import Any, Literal

from common.config import config
from common.logger import logger
from core.config_center.prompts_manager import RAGPromptsManager
from core.rag_model.llm_client import acomplete
from core.rag_retriever.retriever import RetrievedChunk, Retriever
from core.rag_retriever.strategy_selector import RetrievalStrategy


async def _run_prompt(prompt_template: Any, query: str) -> str:
    """把 LangChain PromptTemplate 用 {query} 渲染后送 LLM 补全，返回文本。"""
    text = prompt_template.format(query=query)
    return await acomplete(text, temperature=0.0, max_tokens=config.LLM_MAX_TOKENS)


def _merge_chunks(sets: list[list[RetrievedChunk]], top_k: int) -> list[RetrievedChunk]:
    """多路检索结果合并去重（按 chunk_id，无 id 按内容），保序截断到 top_k。"""
    seen: set[Any] = set()
    merged: list[RetrievedChunk] = []
    for chunk_list in sets:
        for c in chunk_list:
            key = c.chunk_id if c.chunk_id else c.content
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
            if len(merged) >= top_k:
                return merged
    return merged


class QueryAugmenter:
    """检索增强编排：按策略生成增强 query，检索并合并。"""

    def __init__(self, retriever: Retriever | None = None) -> None:
        self.retriever = retriever or Retriever()

    async def _retrieve(self, query: str, **kwargs) -> list[RetrievedChunk]:
        return await self.retriever.retrieve(query=query, **kwargs)

    async def retrieve_direct(self, query: str, **kwargs) -> list[RetrievedChunk]:
        return await self._retrieve(query, **kwargs)

    async def retrieve_hyde(self, query: str, **kwargs) -> list[RetrievedChunk]:
        try:
            hyp_doc = await _run_prompt(RAGPromptsManager.hyde_gen_prompt(), query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'HyDE 生成失败，回退直接检索: {e}')
            return await self.retrieve_direct(query, **kwargs)
        if not hyp_doc:
            return await self.retrieve_direct(query, **kwargs)
        return await self._retrieve(hyp_doc, **kwargs)

    async def retrieve_subqueries(self, query: str, **kwargs) -> list[list[RetrievedChunk]]:
        try:
            raw = await _run_prompt(RAGPromptsManager.subq_gen_prompt(), query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'子查询生成失败，回退直接检索: {e}')
            return [await self.retrieve_direct(query, **kwargs)]
        sub_queries = [ln.strip() for ln in (raw or '').splitlines() if ln.strip()][: config.SUBQUERY_MAX]
        if not sub_queries:
            return [await self.retrieve_direct(query, **kwargs)]
        return [await self._retrieve(sq, **kwargs) for sq in sub_queries]

    async def retrieve_backtrack(self, query: str, **kwargs) -> list[RetrievedChunk]:
        try:
            core_q = await _run_prompt(RAGPromptsManager.back_gen_prompt(), query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'回溯问题生成失败，回退直接检索: {e}')
            return await self.retrieve_direct(query, **kwargs)
        if not core_q:
            return await self.retrieve_direct(query, **kwargs)
        return await self._retrieve(core_q, **kwargs)

    async def augment_and_retrieve(
        self,
        query: str,
        strategy: RetrievalStrategy,
        *,
        tenant_id: int,
        rerank: bool = True,
        scope: Literal["doc", "faq"] = "doc",
    ) -> list[RetrievedChunk]:
        """按策略做检索增强并返回合并后的片段。

        召回 / 精排条数由 config.RECALL_K / config.RERANK_TOP_K 统一控制（不按请求传 top_k）。
        """
        kwargs = dict(tenant_id=tenant_id, rerank=rerank, scope=scope)
        if strategy == RetrievalStrategy.DIRECT:
            result = await self.retrieve_direct(query, **kwargs)
        elif strategy == RetrievalStrategy.HYDE:
            result = await self.retrieve_hyde(query, **kwargs)
        elif strategy == RetrievalStrategy.SUB_QUERY:
            sub_sets = await self.retrieve_subqueries(query, **kwargs)
            result = _merge_chunks(sub_sets, config.RERANK_TOP_K)
        elif strategy == RetrievalStrategy.BACKTRACK:
            result = await self.retrieve_backtrack(query, **kwargs)
        else:
            result = await self.retrieve_direct(query, **kwargs)

        # 高级策略空结果 → 降级 DIRECT，保证检索不空
        if not result and strategy != RetrievalStrategy.DIRECT:
            logger.debug(f'策略 {strategy.value} 检索为空，降级 DIRECT')
            result = await self.retrieve_direct(query, **kwargs)
        return result
