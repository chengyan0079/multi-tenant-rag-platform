"""DocDao 版本化软删 / 回滚 / 列表单测（fake 异步 session，无需真实数据库）。"""
import asyncio

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


_CHUNKS = [{'text': 'a', 'parent_id': 'p', 'parent_content': 'a', 'doc_name': 'f', 'page': 0}]


def _chunk_enabled_updates(stmts: list) -> list[int]:
    return [
        s.compile().params['enabled']
        for s in stmts
        if getattr(s, 'is_update', False) and s.table.name == 'rag_chunk'
        and 'enabled' in s.compile().params
    ]


def test_save_chunks_soft_deletes_old_and_inserts_new_version() -> None:
    doc_dao = _import_dao()
    # 预置：FOR UPDATE 锁行返回 doc id，max(version) 返回 None（首个版本），Doc 更新返回默认
    factory = _FakeFactory(results=[_FakeResult(scalar=1), _FakeResult(scalar=None), _FakeResult()])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        version, n = asyncio.run(doc_dao.DocDao().save_chunks(100, 1, 0, _CHUNKS))
        assert (version, n) == (1, 1)
        sess = factory.last_session
        # 版本分配前先对 Doc 行加 FOR UPDATE 锁，保证并发索引串行化
        assert any('FOR UPDATE' in str(s) for s in sess.stmts), "缺少文档行悲观锁"
        # 旧版本软删：应有一次 SET enabled=0
        assert 0 in _chunk_enabled_updates(sess.stmts), "未对旧版本执行软删"
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
        enabled = asyncio.run(doc_dao.DocDao().rollback_version(100, 1, target_version=2))
        assert enabled == 3
        sess = factory.last_session
        enabled_values = _chunk_enabled_updates(sess.stmts)
        assert 0 in enabled_values, "缺少全量禁用更新"
        assert 1 in enabled_values, "缺少启用目标版本更新"
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]


def test_list_versions_merges_rows() -> None:
    doc_dao = _import_dao()
    # group_by 查询返回 [(version, enabled, count), ...]
    factory = _FakeFactory(results=[_FakeResult(rows=[(1, 1, 3), (1, 0, 0), (2, 0, 2)])])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        versions = asyncio.run(doc_dao.DocDao().list_versions(100, 1))
        by_v = {v['version']: v for v in versions}
        assert by_v[1]['chunk_count'] == 3 and by_v[1]['enabled'] == 1
        assert by_v[2]['chunk_count'] == 2 and by_v[2]['enabled'] == 0
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]


def test_save_chunks_missing_doc_raises() -> None:
    doc_dao = _import_dao()
    factory = _FakeFactory(results=[_FakeResult(scalar=None)])
    orig = doc_dao.get_sessionmaker
    doc_dao.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        with pytest.raises(KeyError):
            asyncio.run(doc_dao.DocDao().save_chunks(100, 1, 0, _CHUNKS))
    finally:
        doc_dao.get_sessionmaker = orig  # type: ignore[assignment]
