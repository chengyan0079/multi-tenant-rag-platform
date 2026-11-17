"""pipeline 单测：mock 外部依赖（file_provider / milvus_store / DocDao / process_documents），

验证 build_index 的切片组装与落库调用、enqueue_index 的 Redis/降级路径、run_index_job 的状态流转。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.app_state import app_state


@pytest.fixture
def idx_env(monkeypatch):
    import store.doc_dao as doc_dao_mod
    monkeypatch.setattr(doc_dao_mod, 'get_sessionmaker', lambda: None)
    saved = {a: getattr(app_state, a) for a in ('file_provider', 'milvus_store', 'redis')}
    app_state.file_provider = SimpleNamespace(get_bytes=AsyncMock(return_value=b'hello\nworld'))
    app_state.milvus_store = SimpleNamespace(add_chunks=AsyncMock(), delete_by_chunk_ids=AsyncMock())
    app_state.redis = None
    yield app_state
    for k, v in saved.items():
        setattr(app_state, k, v)


def _doc(text, cid, pid, pcontent):
    return SimpleNamespace(page_content=text, metadata={'id': cid, 'parent_id': pid, 'parent_content': pcontent})


def test_build_index_assembles_and_stores(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    monkeypatch.setattr(pipeline, 'process_documents', lambda _d: [
        _doc('a text', 'c0', 'p0', 'parent0'),
        _doc('b text', 'c1', 'p0', 'parent0'),
    ])
    saved = []
    async def fake_save(tenant_id, doc_id, scope, chunks, version=1):
        saved.append((tenant_id, doc_id, scope, chunks))
        return len(chunks)
    monkeypatch.setattr(pipeline.DocDao, 'save_chunks', staticmethod(fake_save))
    monkeypatch.setattr(pipeline.DocDao, 'next_version', staticmethod(AsyncMock(return_value=1)))
    monkeypatch.setattr(pipeline.DocDao, 'chunk_ids_of_version', staticmethod(AsyncMock(return_value=[])))

    n = asyncio.run(pipeline.build_index(100, 7, 'oss-k', 'f.pdf', scope=0))
    assert n == 2
    _, _, scope, chunks = saved[0]
    assert scope == 0
    assert chunks[0]['text'] == 'a text' and chunks[0]['parent_id'] == 'p0'
    mc = app_state.milvus_store.add_chunks.call_args[0][0]
    assert len(mc) == 2
    assert mc[0]['chunk_id'] and isinstance(mc[0]['chunk_id'], int)
    assert mc[0]['tenant_id'] == 100 and mc[0]['doc_id'] == 7 and mc[0]['scope'] == 0
    assert mc[1]['text'] == 'b text'


def test_build_index_empty(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    monkeypatch.setattr(pipeline, 'process_documents', lambda _d: [])
    saved = []
    monkeypatch.setattr(pipeline.DocDao, 'save_chunks', staticmethod(
        lambda *a, **k: (saved.append(a) or 0)))
    n = asyncio.run(pipeline.build_index(1, 2, 'k', 'f', scope=1))
    assert n == 0
    assert not saved  # 空内容不落库


def test_enqueue_redis(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    import json
    app_state.redis = AsyncMock()
    asyncio.run(pipeline.enqueue_index(1, 2, 'k', 'f', 0))
    assert app_state.redis.lpush.called
    payload = json.loads(app_state.redis.lpush.call_args[0][1])
    assert payload == {'tenant_id': 1, 'doc_id': 2, 'oss_key': 'k', 'file_name': 'f', 'doc_type': 0}


def test_enqueue_fallback(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    app_state.redis = None
    recorded = []
    monkeypatch.setattr(asyncio, 'create_task', lambda c: recorded.append(c))
    asyncio.run(pipeline.enqueue_index(1, 2, 'k', 'f', 0))
    assert len(recorded) == 1  # 降级走 create_task
    recorded[0].close()  # 避免 coroutine never awaited 警告


def test_run_index_job_success(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    states = []
    async def fake_update(doc_id, status, **kw):
        states.append((status, kw))
    monkeypatch.setattr(pipeline.DocDao, 'update_status', staticmethod(fake_update))
    monkeypatch.setattr(pipeline, 'build_index', AsyncMock(return_value=5))
    asyncio.run(pipeline.run_index_job(1, 9, 'k', 'f', 0))
    assert [s[0] for s in states] == [1, 2]
    assert states[1][1]['chunk_count'] == 5


def test_run_index_job_fail(idx_env, monkeypatch):
    from core.rag_indexer import pipeline
    states = []
    async def fake_update(doc_id, status, **kw):
        states.append((status, kw))
    monkeypatch.setattr(pipeline.DocDao, 'update_status', staticmethod(fake_update))
    async def boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(pipeline, 'build_index', boom)
    asyncio.run(pipeline.run_index_job(1, 9, 'k', 'f', 0))
    assert states[0][0] == 1
    assert states[1][0] == 3 and 'boom' in states[1][1]['err_msg']


def test_run_index_job_review_triggers_graph(idx_env, monkeypatch):
    """doc_type=2 时索引成功后触发入图，scope 仍按普通文档为 0。"""
    from core.rag_indexer import pipeline
    states, scopes = [], []
    async def fake_update(doc_id, status, **kw):
        states.append(status)
    async def fake_build(tid, did, key, name, scope=0):
        scopes.append(scope)
        return 3
    monkeypatch.setattr(pipeline.DocDao, 'update_status', staticmethod(fake_update))
    monkeypatch.setattr(pipeline, 'build_index', fake_build)
    ingest = AsyncMock()
    monkeypatch.setattr(pipeline, 'ingest_review_from_file', ingest)
    asyncio.run(pipeline.run_index_job(1, 9, 'k', 'f', 2))
    assert scopes == [0]
    assert states == [1, 2]
    ingest.assert_awaited_once_with(1, 'k')


def test_run_index_job_graph_failure_not_fail_index(idx_env, monkeypatch):
    """入图失败只告警，不改索引完成状态。"""
    from core.rag_indexer import pipeline
    states = []
    async def fake_update(doc_id, status, **kw):
        states.append(status)
    async def boom(*a, **k):
        raise RuntimeError('graph boom')
    monkeypatch.setattr(pipeline.DocDao, 'update_status', staticmethod(fake_update))
    monkeypatch.setattr(pipeline, 'build_index', AsyncMock(return_value=2))
    monkeypatch.setattr(pipeline, 'ingest_review_from_file', boom)
    asyncio.run(pipeline.run_index_job(1, 9, 'k', 'f', 2))
    assert states == [1, 2]  # 没有 3


def test_run_index_job_faq_scope(idx_env, monkeypatch):
    """doc_type=1 的 scope 仍为 1，不触发入图。"""
    from core.rag_indexer import pipeline
    scopes = []
    async def fake_build(tid, did, key, name, scope=0):
        scopes.append(scope)
        return 1
    monkeypatch.setattr(pipeline.DocDao, 'update_status', staticmethod(AsyncMock()))
    monkeypatch.setattr(pipeline, 'build_index', fake_build)
    ingest = AsyncMock()
    monkeypatch.setattr(pipeline, 'ingest_review_from_file', ingest)
    asyncio.run(pipeline.run_index_job(1, 9, 'k', 'f', 1))
    assert scopes == [1]
    ingest.assert_not_awaited()


def test_decode_text_gbk(monkeypatch):
    """GBK 复盘文本按配置编码顺序解码。"""
    from core.rag_indexer import pipeline
    text = '故障复盘内容'
    data = text.encode('gbk')
    assert pipeline._decode_text(data) == text
