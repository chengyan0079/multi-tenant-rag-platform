"""PostgresManager 单测：直连目标库探测 + 错误码分类（3D000=库不存在 / 其它=连接失败）。

用 mock 引擎替换 create_async_engine，不依赖真实 PostgreSQL，也不依赖 greenlet；
若当前解释器无法导入 sqlalchemy.ext.asyncio（缺 greenlet），测试自动 skip。
不再查询 information_schema（按权限过滤、非 superuser 易误判）。
"""
import asyncio
from unittest import mock

import pytest

from app.app_state import app_state


def _import_manager():
    """导入 PostgresManager；greenlet 缺失时跳过整个用例。"""
    try:
        from app.managers.postgres_manager import PostgresManager
        import app.managers.postgres_manager as mod
        return PostgresManager, mod
    except ImportError:
        pytest.skip("sqlalchemy.ext.asyncio 不可用（缺 greenlet），跳过 PostgresManager 单测")


class _UnknownDBError(Exception):
    """模拟 PostgreSQL sqlstate 3D000（库不存在）。

    asyncpg 错误带 sqlstate 属性；SQLAlchemy 包裹后通过 exc.orig 透出。
    """

    @property
    def sqlstate(self):
        return '3D000'

    def __init__(self) -> None:
        super().__init__("database \"rag_ultra\" does not exist")


class _Conn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, clause, params=None):
        return None  # SELECT 1 探活，不使用结果


class _Engine:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.disposed = False

    def connect(self):
        if self._error is not None:
            raise self._error
        return _Conn()

    async def dispose(self):
        self.disposed = True


def _factory(error: Exception | None = None):
    def _f(dsn, **kw):
        return _Engine(error)
    return _f


def test_postgres_start_ok() -> None:
    PostgresManager, mod = _import_manager()
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(None)):
            asyncio.run(PostgresManager().start())
        assert app_state.db_engine is not None
    finally:
        app_state.db_engine = None


def test_postgres_start_fails_when_db_missing() -> None:
    PostgresManager, mod = _import_manager()
    from app.server import StartupError
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(_UnknownDBError())):
            with pytest.raises(StartupError):
                asyncio.run(PostgresManager().start())
        assert app_state.db_engine is None   # 失败后清理，不留半初始化引擎
    finally:
        app_state.db_engine = None


def test_postgres_start_fails_when_cannot_connect() -> None:
    PostgresManager, mod = _import_manager()
    from app.server import StartupError
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(RuntimeError("connect failed"))):
            with pytest.raises(StartupError):
                asyncio.run(PostgresManager().start())
        assert app_state.db_engine is None
    finally:
        app_state.db_engine = None


def test_is_unknown_database() -> None:
    _, mod = _import_manager()
    # 直接 DBAPI 异常：sqlstate == 3D000
    assert mod._is_unknown_database(_UnknownDBError()) is True
    # 仅消息含 "does not exist"（无 sqlstate 的退化匹配）
    assert mod._is_unknown_database(Exception("database \"x\" does not exist")) is True
    # SQLAlchemy 包裹形态：exc.orig.sqlstate == 3D000
    wrapped = Exception("wrapper")
    wrapped.orig = _UnknownDBError()
    assert mod._is_unknown_database(wrapped) is True
    # 其它错误（连不上 / 无权限）→ False
    assert mod._is_unknown_database(RuntimeError("connect failed")) is False
    assert mod._is_unknown_database(Exception("permission denied for database")) is False
