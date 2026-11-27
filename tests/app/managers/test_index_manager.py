"""IndexManager 崩溃恢复单测：扫描 status∈{0,1}、SETNX 认领避免多 worker 重复重投。"""
import asyncio
import json
from unittest import mock

from app.app_state import app_state


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FileRow:
    current_oss_key = '/oss/k.pdf'
    file_name = 'f.pdf'


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        if 'rag_document' in text and 'status' in text:
            return _Result(self._rows)
        return _Result([_FileRow()])  # File 查询返回具名行


class _Redis:
    def __init__(self, claimed: set[int] | None = None):
        self.claimed = claimed if claimed is not None else set()
        self.pushed: list[dict] = []

    async def set(self, key, value, nx=False, ex=None):
        doc_id = int(key.rsplit(':', 1)[-1])
        if doc_id in self.claimed:
            return False
        self.claimed.add(doc_id)
        return True

    async def lpush(self, key, payload):
        self.pushed.append(json.loads(payload))


def _run_requeue(rows, redis, monkeypatch):
    from app.managers import index_manager as im

    monkeypatch.setattr(im, 'get_sessionmaker', lambda: lambda: _Session(rows))
    saved = app_state.redis
    app_state.redis = redis
    try:
        asyncio.run(im.IndexManager()._requeue_pending())
    finally:
        app_state.redis = saved


def test_requeue_scans_pending_and_processing(monkeypatch):
    """status=1（worker 硬崩溃遗留）与 status=0 都要重投。"""
    rows = [(1, 100, 10, 0), (2, 100, 11, 1)]
    redis = _Redis()
    _run_requeue(rows, redis, monkeypatch)

    assert len(redis.pushed) == 2
    assert {j['doc_id'] for j in redis.pushed} == {1, 2}
    assert all(j['tenant_id'] == 100 and j['file_name'] == 'f.pdf' for j in redis.pushed)


def test_requeue_skips_already_claimed(monkeypatch):
    """另一个 worker 已认领的文档不重复入队。"""
    rows = [(1, 100, 10, 0), (2, 100, 11, 1)]
    redis = _Redis(claimed={1})
    _run_requeue(rows, redis, monkeypatch)

    assert [j['doc_id'] for j in redis.pushed] == [2]


def test_requeue_skips_missing_oss_key(monkeypatch):
    """File 记录缺失时跳过，不投空任务。"""
    from app.managers import index_manager as im

    class _NoFileSession(_Session):
        async def execute(self, stmt):
            if 'rag_document' in str(stmt):
                return _Result([(1, 100, 10, 0)])
            return _Result([])  # File 未命中

    monkeypatch.setattr(im, 'get_sessionmaker', lambda: lambda: _NoFileSession([]))
    redis = _Redis()
    saved = app_state.redis
    app_state.redis = redis
    try:
        asyncio.run(im.IndexManager()._requeue_pending())
    finally:
        app_state.redis = saved
    assert redis.pushed == []
