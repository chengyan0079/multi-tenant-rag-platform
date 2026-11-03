"""DocDao 版本化软删 / 回滚 / 列表 单测（fake 异步 session，无需真实数据库）。"""
import sys
import types

import pytest


def _import_dao():
    try:
        from store import doc_dao as mod
        return mod
    except ImportError:
        pytest.skip("pymilvus / 依赖不可用，跳过 DocDao 测试")


class _FakeResult:
    def __init__(self, scalar=None, rows=None, rowcount=0):
        self._scalar = scalar
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, results=None):
        self.stmts = []
        self.added = []
        self._results = list(results or [])
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    def add_all(self, rows):
        self.added.extend(rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeFactory:
    def __init__(self, results=None):
        self._results = results
        self.last_session = None

    def __call__(self):
        self.last_session = _FakeSession(self._results)
        return self.last_session


def _make_chunk(**kw):
    from store.orm_models.knowledge import Chunk
    return Chunk(**kw)


def test_save_chunks_soft_deletes_old_and_inserts_new_version() -> None:
    doc_dao = _import_dao()
    # 预置：next_version 查询返回 1（无旧切片），Doc 更新返回默认
    factory = _FakeFactory(results=[_FakeResult(scalar=None), _FakeResult()])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        chunks = [{'text': 'a', 'parent_id': 'p', 'parent_content': 'a', 'doc_name': 'f', 'page': 0}]
        n = doc_dao.DocDao().save_chunks(100, 1, 0, chunks, version=1)
        assert n == 1
        sess = factory.last_session
        # 旧版本软删：应有一次 UPDATE ... SET enabled=0
        soft_delete = [s for s in sess.stmts if 'enabled=0' in str(s)]
        assert soft_delete, "未对旧版本执行软删"
        # 新切片写入且启用
        assert len(sess.added) == 1
        c = sess.added[0]
        assert c.version == 1
        assert c.enabled == 1
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]


def test_rollback_version_issues_enable_disable_updates() -> None:
    doc_dao = _import_dao()
    # 三次 execute：全量禁用 / 启用目标版本 / Doc 版本更新；前两次返回默认，第三次 rowcount=3
    factory = _FakeFactory(results=[_FakeResult(), _FakeResult(rowcount=3), _FakeResult()])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        enabled = doc_dao.DocDao().rollback_version(100, 1, target_version=2)
        assert enabled == 3
        sess = factory.last_session
        stmts_str = [str(s) for s in sess.stmts]
        assert any('enabled=0' in s for s in stmts_str), "缺少全量禁用更新"
        assert any('enabled=1' in s for s in stmts_str), "缺少启用目标版本更新"
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]


def test_list_versions_merges_rows() -> None:
    doc_dao = _import_dao()
    # group_by 查询返回 [(version, enabled, count), ...]
    factory = _FakeFactory(results=[_FakeResult(rows=[(1, 1, 3), (1, 0, 0), (2, 0, 2)])])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        versions = doc_dao.DocDao().list_versions(100, 1)
        by_v = {v['version']: v for v in versions}
        assert by_v[1]['chunk_count'] == 3 and by_v[1]['enabled'] == 1
        assert by_v[2]['chunk_count'] == 2 and by_v[2]['enabled'] == 0
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]


def test_next_version_returns_one_when_empty() -> None:
    doc_dao = _import_dao()
    factory = _FakeFactory(results=[_FakeResult(scalar=None)])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        assert doc_dao.DocDao().next_version(100, 1) == 1
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]
