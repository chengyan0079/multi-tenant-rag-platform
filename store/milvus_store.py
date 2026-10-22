"""
milvus_store - 向量存储（M3）

集合字段：chunk_id(主键) / tenant_id(分区键) / scope / doc_id / ref_id /
dense(1024 维, 内积 IP + L2 归一化 == 余弦) / sparse(倒排, 内积)。
注意：dense 度量由 COSINE 改为 IP（对齐 rag-pro）。已按 COSINE 建索引的旧集合需
删除后重启服务会自动重建（度量在索引创建时固定，无法原地改），并重新灌库。
检索时按 tenant_id + scope 过滤，再做 dense、sparse 两路召回并融合。
使用 pymilvus 的 AsyncMilvusClient，全部 IO 异步，不阻塞事件循环。
（文档/切片为租户级，不再挂知识库，故无 kb_id 维度。）
"""
import asyncio
from typing import Any

import numpy as np

from pymilvus import (
    AnnSearchRequest, AsyncMilvusClient, DataType, FieldSchema, CollectionSchema,
    RRFRanker, WeightedRanker,
)

from common.config import config
from common.logger import logger, setup_logging
from core.rag_model.model_service import ModelService


def _parse_sparse_row(sparse_matrix: Any, index: int) -> dict[int, float]:
    """把 BGEM3 返回的稀疏矩阵某一行转成 {token_id: 权重}。"""
    row = sparse_matrix[index]
    if hasattr(row, 'tocoo'):
        coo = row.tocoo()
        return {int(col): float(val) for col, val in zip(coo.col, coo.data)}
    elif hasattr(row, 'col') and hasattr(row, 'data'):
        return {int(col): float(val) for col, val in zip(row.col, row.data)}
    return {}


def _l2_normalize(vec: Any):
    """L2 归一化稠密向量：使内积(IP)等价于余弦(COSINE)，与 rag-pro 的 IP 度量对齐。

    写入(add_chunks)与查询(hybrid_search)都归一化，保证两侧一致；已归一化则为恒等操作。
    """
    arr = np.asarray(vec, dtype='float32')
    norm = float(np.linalg.norm(arr))
    return arr if norm == 0.0 else (arr / norm)


class MilvusStore:
    """dense + sparse 双向量存储（异步）。"""

    def __init__(self, client: AsyncMilvusClient | None = None) -> None:
        self.client = client
        self.model: ModelService | None = None

    def bind(self, client: AsyncMilvusClient, model: ModelService) -> None:
        """绑定客户端与模型服务；ensure/add 需要 model 的 dense_dim。"""
        self.client = client
        self.model = model

    async def ensure_collection(self) -> None:
        """确保集合就绪：不存在则创建集合与索引，已存在则直接加载（启动时由 MilvusManager 调用，幂等）。"""
        name = config.MILVUS_COLLECTION
        if self.client is None:
            raise RuntimeError('MilvusClient 未绑定')
        if not await self.client.has_collection(name):
            if self.model is None:
                raise RuntimeError('ModelService 未绑定，无法确定向量维度')
            schema = CollectionSchema(
                fields=[
                    FieldSchema(name='chunk_id', dtype=DataType.INT64, is_primary=True, auto_id=False),
                    FieldSchema(name='tenant_id', dtype=DataType.INT64, is_partition_key=True),
                    FieldSchema(name='scope', dtype=DataType.INT8),
                    FieldSchema(name='doc_id', dtype=DataType.INT64),
                    FieldSchema(name='ref_id', dtype=DataType.INT64),
                    FieldSchema(name='dense', dtype=DataType.FLOAT_VECTOR, dim=self.model.dense_dim),
                    FieldSchema(name='sparse', dtype=DataType.SPARSE_FLOAT_VECTOR),
                    FieldSchema(name='text', dtype=DataType.VARCHAR, max_length=65535),
                    FieldSchema(name='parent_id', dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name='parent_content', dtype=DataType.VARCHAR, max_length=65535),
                    FieldSchema(name='doc_name', dtype=DataType.VARCHAR, max_length=256),
                    FieldSchema(name='page', dtype=DataType.INT32),
                ],
                enable_dynamic_field=True,
            )
            index_params = self.client.prepare_index_params()  # 本地构建，同步无需 await
            index_params.add_index(
                field_name='dense', index_name='dense_index',
                index_type='HNSW', metric_type='IP',  # IP + 归一化 == 余弦（对齐 rag-pro）
                params={'M': 16, 'efConstruction': 128},
            )
            index_params.add_index(
                field_name='sparse', index_name='sparse_index',
                index_type='SPARSE_INVERTED_INDEX', metric_type='IP',
                params={'drop_ratio_build': 0.2},
            )
            await self.client.create_collection(name, schema=schema, index_params=index_params)
            logger.info(f'创建集合 [{name}] 并建立索引')
        else:
            logger.info(f'集合 [{name}] 已存在')
        await self.client.load_collection(name)
        logger.info(f'集合 [{name}] 已加载到内存')

    async def add_chunks(self, chunks: list[dict[str, Any]], batch_size: int = 32) -> None:
        """批量写入向量（预留的写入能力）。chunks 需含 chunk_id/tenant_id/scope/text 等字段。"""
        if not chunks:
            return
        if self.model is None:
            raise RuntimeError('ModelService 未绑定，无法向量化')
        total = len(chunks)
        for i in range(0, total, batch_size):
            batch = chunks[i:i + batch_size]
            texts = [c['text'] for c in batch]
            embeddings = await asyncio.to_thread(self.model.embed, texts)
            data = []
            for idx, c in enumerate(batch):
                data.append({
                    'chunk_id': c['chunk_id'],
                    'tenant_id': c['tenant_id'],
                    'scope': c['scope'],
                    'doc_id': c.get('doc_id', 0),
                    'ref_id': c.get('ref_id', 0),
                    'dense': _l2_normalize(embeddings['dense'][idx]),
                    'sparse': _parse_sparse_row(embeddings['sparse'], idx),
                    'text': c['text'],
                    'parent_id': c.get('parent_id', ''),
                    'parent_content': c.get('parent_content', c['text']),
                    'doc_name': c.get('doc_name', ''),
                    'page': c.get('page', 0),
                })
            await self.client.upsert(collection_name=config.MILVUS_COLLECTION, data=data)
            logger.debug(f'批次 [{i // batch_size + 1}] 写入 {len(data)} 条向量')
        logger.info(f'向量写入完成，共 {total} 条')

    async def hybrid_search(
        self,
        query: str,
        scope: int,
        tenant_id: int,
        top_k: int = 20,
        fusion: str = 'rrf',
    ) -> list[dict[str, Any]]:
        """两路召回 + 融合。scope 区分文档库(0)与 FAQ 库(1)；tenant_id 做租户隔离。"""
        if not query or not query.strip():
            return []
        if self.model is None:
            raise RuntimeError('ModelService 未绑定，无法向量化查询')

        q_emb = await asyncio.to_thread(self.model.embed, [str(query)])
        dense_vec = _l2_normalize(q_emb['dense'][0])
        sparse_vec = _parse_sparse_row(q_emb['sparse'], 0)

        filter_expr = f'tenant_id == {tenant_id} and scope == {scope}'
        dense_req = AnnSearchRequest(
            data=[dense_vec], anns_field='dense',
            param={'metric_type': 'IP', 'params': {'ef': 64}},
            limit=top_k, expr=filter_expr,
        )
        sparse_req = AnnSearchRequest(
            data=[sparse_vec], anns_field='sparse',
            param={'metric_type': 'IP', 'params': {'drop_ratio_search': 0.2}},
            limit=top_k, expr=filter_expr,
        )
        ranker = (
            WeightedRanker(config.DENSE_WEIGHT, config.SPARSE_WEIGHT)
            if fusion == 'weighted' else RRFRanker(k=config.RRF_K)
        )
        results = await self.client.hybrid_search(
            collection_name=config.MILVUS_COLLECTION,
            reqs=[dense_req, sparse_req],
            ranker=ranker,
            limit=top_k,
            output_fields=['text', 'parent_id', 'parent_content', 'doc_name', 'page', 'chunk_id', 'ref_id'],
        )
        if not results or not results[0]:
            return []
        return [hit['entity'] for hit in results[0]]


if __name__ == '__main__':
    setup_logging()
    store = MilvusStore()
    # store.bind(client, ModelService())
    # hits = asyncio.run(store.hybrid_search('学习AI大模型课程能够掌握哪些核心竞争力？', scope=0, tenant_id=1))
    # print(hits)
