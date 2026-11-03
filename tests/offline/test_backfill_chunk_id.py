"""scripts.backfill_chunk_id 单测：用假 session 验证复算与批量 UPDATE，不连真实库。"""
import argparse
import asyncio

from common.config import config  # noqa: F401  依赖缺失时下方 import 会 skip


def _import_mod():
    try:
        from scripts import backfill_chunk_id
        return backfill_chunk_id
    except ImportError:
        import pytest
        pytest.skip("backfill_chunk_id 依赖不可用，跳过")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows, captured):
        self._rows = list(rows)
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        if params is not None:  # 批量 UPDATE（mappings 列表）
            self._captured.extend(params)
            return _FakeResult([])
        # SELECT：第一次返回批次，之后清空以结束循环
        rows, self._rows = self._rows, []
        return _FakeResult(rows)

    async def commit(self):
        pass


def _args(**kw):
    ns = argparse.Namespace()
    ns.tenant = kw.get('tenant', 0)
    ns.batch = kw.get('batch', 1000)
    ns.dry_run = kw.get('dry_run', False)
    return ns


def test_backfill_recomputes_chunk_id_and_updates() -> None:
    mod = _import_mod()
    try:
        from store.milvus_store import stable_chunk_id
    except ImportError:
        import pytest
        pytest.skip("milvus_store 依赖不可用，跳过")
    import store.postgres_store as pg

    # 两条 chunk_id=0 的历史行：不同 (tenant, doc, version, parent, seq)
    rows = [
        (10, 1, 100, 1, 0, {'parent_id': 'p1'}),
        (11, 1, 100, 1, 1, {'parent_id': ''}),
    ]
    captured: list[dict] = []

    pg.get_sessionmaker = lambda: _FakeSession(rows, captured)
    asyncio.run(mod._main_async(_args()))

    assert len(captured) == 2
    by_id = {m['id']: m['chunk_id'] for m in captured}
    assert by_id[10] == stable_chunk_id(1, 100, 1, 'p1', 0)
    assert by_id[11] == stable_chunk_id(1, 100, 1, '', 1)


def test_backfill_dry_run_does_not_update() -> None:
    mod = _import_mod()
    import store.postgres_store as pg

    rows = [(12, 2, 200, 1, 0, {'parent_id': 'x'})]
    captured: list[dict] = []
    pg.get_sessionmaker = lambda: _FakeSession(rows, captured)
    asyncio.run(mod._main_async(_args(dry_run=True)))

    assert captured == []  # dry-run 不发起 UPDATE
