"""图证据与向量结果的外层 RRF 融合 + rerank 精排 + 链补齐。

内层 RRF 在 Milvus 里融合 dense/sparse；外层这里融合异构的图证据与向量候选。
图证据没有可比分数，只能用排名融合（RRF），不编造相似度做加权。
"""
from __future__ import annotations

import asyncio
from typing import Any

from common.config import config
from common.logger import logger
from core.graph.render import refill_chain
from core.rag_retriever.retriever import RetrievedChunk


def rrf_fuse(ranked_lists: list[list[RetrievedChunk]],
             k: int | None = None,
             weights: list[float] | None = None) -> list[RetrievedChunk]:
    """多路 ranked list 融合；去重键用 (doc_name, chunk_id, 内容前 32 字)。

    图证据与向量 chunk 的 id 空间不同，不能直接用 chunk_id 作 key。
    """
    k = k if k is not None else config.RRF_K
    w = weights or [1.0] * len(ranked_lists)
    scores: dict[str, float] = {}
    store: dict[str, RetrievedChunk] = {}
    for ranked, weight in zip(ranked_lists, w):
        for rank, item in enumerate(ranked, start=1):
            key = f'{item.doc_name}|{item.chunk_id}|{item.content[:32]}'
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            store.setdefault(key, item)
    for key, score in scores.items():
        store[key].score = score
    return sorted(store.values(), key=lambda c: -c.score)


def rerank_and_refill(query: str, cands: list[RetrievedChunk], model: Any,
                      top_k: int | None = None) -> list[RetrievedChunk]:
    """rerank 选点 → 按 chain_id 补齐兄弟证据 → 保证因果链完整。

    model 为 ModelService；rerank 失败或模型未加载时退回融合顺序，与现有行为一致。
    """
    if not cands:
        return []
    top_k = top_k if top_k is not None else config.RERANK_TOP_K
    if model is None or getattr(model, 'reranker', None) is None:
        return cands[:top_k]
    try:
        results = model.rerank(query=query, documents=[c.content for c in cands], top_k=top_k)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'图向量融合 rerank 失败，退回 RRF 顺序: {e}')
        return cands[:top_k]
    picked = [cands[r.index] for r in results]
    for c, r in zip(picked, results):
        c.rerank_score = float(r.score)
    return refill_chain(cands, picked)


async def graph_fused_retrieve(
    query: str,
    tenant_id: int,
    qtype: str,
    query_params: dict[str, Any],
    vector_hits: list[RetrievedChunk],
    model: Any,
    graph_search_fn: Any,
    render_fn: Any,
    weights: list[float] | None = None,
) -> list[RetrievedChunk] | None:
    """图路 + 向量路融合入口；图路空/超时/异常都返回 None，调用方退回纯向量。

    查询与渲染函数由调用方注入，便于单测隔离且避免导入期拉起 Neo4j 依赖。
    """
    try:
        # 图路硬超时：不能因为图慢就把用户请求拖死
        rows = await asyncio.wait_for(
            graph_search_fn(qtype, tenant_id, **query_params),
            timeout=config.GRAPH_QUERY_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f'图路查询超时（>{config.GRAPH_QUERY_TIMEOUT}s），退化为纯向量')
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f'图路查询失败，退化为纯向量: {e}')
        return None

    if not rows:
        return None
    graph_hits = render_fn(qtype, rows)
    if not graph_hits:
        return None
    graph_hits = graph_hits[:config.GRAPH_MAX_EVIDENCE]
    fused = rrf_fuse([graph_hits, vector_hits], weights=weights)
    return rerank_and_refill(query, fused, model)
