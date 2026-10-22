"""
retriever - 检索编排（M4）

实现"父子块去重 + 高亮"，封装为设计文档 §6.3 的 Retriever。
支持 scope(doc/faq)、RRF 融合、Rerank 重排；Rerank 失败时退回只用 RRF 的结果。
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from common.config import config
from common.logger import logger
from app.app_state import app_state
from core.rag_model.model_service import ModelService
from store.milvus_store import MilvusStore


@dataclass
class RetrievedChunk:
    """一次检索命中的片段，携带来源信息与排序分数。"""
    chunk_id: int
    content: str
    doc_name: str = ''
    page: int = 0
    oss_key: str = ''
    score: float = 0.0
    rerank_score: float = 0.0
    parent_id: str = ''
    meta: dict[str, Any] = field(default_factory=dict)


def _dedup_parent_with_highlight(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按父块去重，并把命中的子块用标签包起来，方便前端做片段定位。

    同一父块下多个子块命中时，只在父块文本里把对应子块内容用
    <检索定位区块> 标签标出，避免把整段父块重复返回。
    """
    parent_to_info: dict[str, dict[str, Any]] = {}
    for hit in hits:
        parent_id = hit.get('parent_id', '')
        parent_content = hit.get('parent_content', hit.get('text', ''))
        child_text = hit.get('text', '')
        # 用 parent_id 或整段内容作去重键，避免同一父块多次返回
        dedup_key = parent_id or parent_content
        if not dedup_key:
            continue
        if dedup_key not in parent_to_info:
            parent_to_info[dedup_key] = {
                'parent_content': parent_content,
                'meta': {
                    'chunk_id': hit.get('chunk_id'),
                    'doc_name': hit.get('doc_name', ''),
                    'page': hit.get('page', 0),
                    'oss_key': hit.get('oss_key', ''),
                    'parent_id': parent_id,
                    'ref_id': hit.get('ref_id', 0),
                },
                'matched_children': [],
            }
        if child_text and child_text not in parent_to_info[dedup_key]['matched_children']:
            parent_to_info[dedup_key]['matched_children'].append(child_text)

    unique: list[dict[str, Any]] = []
    for info in parent_to_info.values():
        content = info['parent_content']
        for child_text in info['matched_children']:
            if child_text in content:
                block = f'\n<检索定位区块>\n{child_text}\n</检索定位区块>\n'
                content = content.replace(child_text, block)
        unique.append({'content': content, 'meta': info['meta']})
    return unique


class Retriever:
    """混合检索编排：向量两路召回 → 融合 → 重排。"""

    def __init__(
        self,
        store: MilvusStore | None = None,
        model: ModelService | None = None,
    ) -> None:
        self.store = store or app_state.milvus_store
        self.model = model or app_state.model

    async def retrieve(
        self,
        query: str,
        tenant_id: int,
        scope: Literal["doc", "faq"] = "doc",
        top_k: int = config.RERANK_TOP_K,
        recall_k: int = config.RECALL_K,
        fusion: Literal["rrf", "weighted"] = "rrf",
        rerank: bool = True,
        session_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """执行混合检索（M4 只检索，不触发生成）。

        :param scope: doc 走文档库检索，faq 走 FAQ 库检索
        :param recall_k: 两路召回后融合保留的候选条数
        :param top_k: 重排后最终返回的条数
        :param rerank: 是否做交叉编码重排，关闭或失败时退回融合结果
        """
        scope_int = 1 if scope == "faq" else 0
        hits = await self.store.hybrid_search(
            query=query, scope=scope_int, tenant_id=tenant_id,
            top_k=recall_k, fusion=fusion,
        )
        if not hits:
            return []

        parents = _dedup_parent_with_highlight(hits)

        # 有重排模型时做精排；失败则降级为只用 RRF 融合结果
        if rerank and self.model and self.model.reranker is not None:
            try:
                rerank_results = self.model.rerank(
                    query=query, documents=[p['content'] for p in parents], top_k=top_k
                )
                chunks: list[RetrievedChunk] = []
                for res in rerank_results:
                    p = parents[res.index]
                    chunks.append(RetrievedChunk(
                        chunk_id=p['meta'].get('chunk_id', 0),
                        content=p['content'],
                        doc_name=p['meta'].get('doc_name', ''),
                        page=p['meta'].get('page', 0),
                        oss_key=p['meta'].get('oss_key', ''),
                        rerank_score=float(res.score),
                        parent_id=p['meta'].get('parent_id', ''),
                        meta=p['meta'],
                    ))
                return chunks
            except Exception as e:
                logger.warning(f'Rerank 失败，退回 RRF 融合结果: {e}')

        # 降级：取前 top_k 个父块（无重排分数）
        chunks = []
        for p in parents[:top_k]:
            chunks.append(RetrievedChunk(
                chunk_id=p['meta'].get('chunk_id', 0),
                content=p['content'],
                doc_name=p['meta'].get('doc_name', ''),
                page=p['meta'].get('page', 0),
                oss_key=p['meta'].get('oss_key', ''),
                parent_id=p['meta'].get('parent_id', ''),
                meta=p['meta'],
            ))
        return chunks
