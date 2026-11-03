"""rag_indexer.pipeline - 单文档索引聚合入口（把现有组件串成"加载→切分→落库"）。

流程：取对象字节 → 落临时目录 → 父子块切分（复用 document_processor.process_documents）
→ 组装 chunk dict → 写 PostgreSQL rag_chunk（DocDao.save_chunks）→ 向量化 + 写 Milvus
（MilvusStore.add_chunks，内部 embed + upsert）。索引状态回写由 run_index_job 统一处理。

没有独立 Worker 进程：消费侧是 app/managers/index_manager（Redis 队列 BRPOP）。
后续换 Celery/ARQ 只需替换 enqueue_index 的投递与 IndexManager 的消费，本模块不变。
"""
import json
import os
import shutil
import tempfile
from typing import Any

from common.logger import logger

from app.app_state import app_state
from core.rag_indexer.document_processor import process_documents
from store.doc_dao import DocDao
from store.milvus_store import stable_chunk_id
from store.redis_keys import index_queue_key


def _ext(file_name: str) -> str:
    return os.path.splitext(file_name)[1].lower() if (file_name and '.' in file_name) else '.bin'


async def build_index(tenant_id: int, doc_id: int, oss_key: str, file_name: str,
                      scope: int = 0) -> int:
    """对单个文档完成切分 + 落库，返回切片数；异常向上抛，由 run_index_job 捕获回写状态。"""
    provider = app_state.file_provider
    if provider is None:
        raise RuntimeError('file_provider 未初始化')
    data = await provider.get_bytes(oss_key, tenant_id=tenant_id)

    tmp_dir = tempfile.mkdtemp(prefix='rag_idx_')
    tmp_path = os.path.join(tmp_dir, f'd{doc_id}{_ext(file_name)}')
    with open(tmp_path, 'wb') as fh:
        fh.write(data)
    try:
        # 复用现有父子块切分流水线（单文件目录等价于一个文档）
        docs = process_documents(tmp_dir)
        if not docs:
            logger.warning(f'文档无可用内容，跳过索引 [doc_id={doc_id}]')
            return 0

        chunks: list[dict[str, Any]] = []
        for i, d in enumerate(docs):
            meta = d.metadata or {}
            chunks.append({
                'text': d.page_content,
                'parent_id': meta.get('parent_id', ''),
                'parent_content': meta.get('parent_content', d.page_content),
                'doc_name': file_name,
                'page': 0,
                'ref_id': 0,
            })

        dao = DocDao()
        # 先取下一个版本号，保证 PG 与 Milvus 主键（含 version）一致
        version = await dao.next_version(tenant_id, doc_id)
        old_version = version - 1

        # 落 PostgreSQL 元数据：旧版本 soft-delete，新版本插入（幂等 + 可回滚）
        await dao.save_chunks(tenant_id, doc_id, scope, chunks, version=version)
        # 向量化 + 写 Milvus（add_chunks 内部 embed + upsert；chunk_id 含 version 互不冲突）
        store = app_state.milvus_store
        if store is None:
            raise RuntimeError('milvus_store 未初始化')
        await store.add_chunks([
            {
                'chunk_id': stable_chunk_id(tenant_id, doc_id, version, c.get('parent_id', ''), i),
                'tenant_id': tenant_id,
                'scope': scope,
                'doc_id': doc_id,
                'ref_id': 0,
                'text': c['text'],
                'parent_id': c.get('parent_id', ''),
                'parent_content': c.get('parent_content', c['text']),
                'doc_name': c.get('doc_name', ''),
                'page': c.get('page', 0),
            }
            for i, c in enumerate(chunks)
        ])
        # 清理旧版本向量实体，Milvus 只保留生效版本（回滚时由调用方重新写入目标版本）
        old_ids = await dao.chunk_ids_of_version(tenant_id, doc_id, old_version)
        await store.delete_by_chunk_ids(old_ids)
        return len(chunks)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_index_job(tenant_id: int, doc_id: int, oss_key: str, file_name: str,
                        doc_type: int = 0) -> None:
    """执行一次索引任务并回写状态：0 待解析 → 1 解析中 → 2 完成 / 3 失败。"""
    dao = DocDao()
    try:
        await dao.update_status(doc_id, 1)
        # doc_type 0=普通文档 1=FAQ 来源，对应切片 scope（0/1）
        n = await build_index(tenant_id, doc_id, oss_key, file_name, scope=doc_type)
        await dao.update_status(doc_id, 2, chunk_count=n)
        logger.info(f'文档索引完成 [doc_id={doc_id} tenant={tenant_id} chunks={n}]')
    except Exception as e:  # noqa: BLE001
        logger.exception(f'文档索引失败 [doc_id={doc_id} tenant={tenant_id}]')
        await dao.update_status(doc_id, 3, err_msg=f'索引失败：{e}'[:512])


async def rollback_index_version(tenant_id: int, doc_id: int, target_version: int) -> int:
    """版本回滚：把文档检索内容恢复到指定历史版本。

    步骤：先把目标版本切片重新写入 Milvus（upsert，chunk_id 含 target_version），
    再删除当前生效版本的 Milvus 实体，最后 PG 侧切换启用版本。先增后删避免检索空窗。
    返回目标版本启用切片数。
    """
    dao = DocDao()
    store = app_state.milvus_store
    if store is None:
        raise RuntimeError('milvus_store 未初始化')

    target_chunks = await dao.get_version_chunks(tenant_id, doc_id, target_version)
    if not target_chunks:
        raise ValueError(f'目标版本无切片，无法回滚: doc={doc_id} version={target_version}')

    # 当前生效版本：从文档记录取（回滚前仍是它）；scope 按文档类型对齐，避免 FAQ 文档错配
    current = await dao.get_status(doc_id)
    current_version = current.get('version', target_version)
    scope = 0 if current.get('doc_type', 0) == 0 else 1

    # 1) 写入目标版本向量（覆盖式 upsert，不依赖是否曾删除）
    await store.add_chunks([
        {
            'chunk_id': stable_chunk_id(tenant_id, doc_id, target_version, c.get('parent_id', ''), c['seq']),
            'tenant_id': tenant_id,
            'scope': scope,
            'doc_id': doc_id,
            'ref_id': 0,
            'text': c['text'],
            'parent_id': c.get('parent_id', ''),
            'parent_content': c.get('parent_content', c['text']),
            'doc_name': c.get('doc_name', ''),
            'page': c.get('page', 0),
        }
        for c in target_chunks
    ])
    # 2) 删除当前版本向量（若与目標相同则跳过，避免自删）
    if current_version != target_version:
        current_ids = await dao.chunk_ids_of_version(tenant_id, doc_id, current_version)
        await store.delete_by_chunk_ids(current_ids)
    # 3) PG 侧切换启用版本
    enabled = await dao.rollback_version(tenant_id, doc_id, target_version)
    logger.info(f'索引版本回滚完成 [doc_id={doc_id} → version={target_version}, chunks={enabled}]')
    return enabled


async def enqueue_index(tenant_id: int, doc_id: int, oss_key: str, file_name: str,
                        doc_type: int = 0) -> None:
    """投递索引任务：优先 Redis 队列（多 worker 不重复消费），不可用则进程内后台执行。"""
    payload = {
        'tenant_id': tenant_id, 'doc_id': doc_id, 'oss_key': oss_key,
        'file_name': file_name, 'doc_type': doc_type,
    }
    redis = app_state.redis
    if redis is not None:
        try:
            await redis.lpush(index_queue_key(), json.dumps(payload, ensure_ascii=False))
            return
        except Exception:  # noqa: BLE001
            logger.warning('索引任务入队失败，降级为进程内执行')
    # 降级：直接后台跑（单进程自测 / Redis 不可用时）
    import asyncio
    asyncio.create_task(
        run_index_job(tenant_id, doc_id, oss_key, file_name, doc_type))
