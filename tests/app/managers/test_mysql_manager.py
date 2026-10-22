"""MySQLManager 单测：直连目标库探测 + 错误码分类（1049=库不存在 / 其它=连接失败）。

用 mock 引擎替换 create_async_engine，不依赖真实 MySQL，也不依赖 greenlet；
若当前解释器无法导入 sqlalchemy.ext.asyncio（缺 greenlet），测试自动 skip。
不再查询 information_schema.schemata（按权限过滤、非 root 易误判）。
"""
import asyncio
from unittest import mock

import pytest

from app.app_state import app_state


def _import_manager():
    """导入 MySQLManager；greenlet 缺失时跳过整个用例。"""
    try:
        from app.managers.mysql_manager import MySQLManager
        import app.managers.mysql_manager as mod
        return MySQLManager, mod
    except ImportError:
        pytest.skip("sqlalchemy.ext.asyncio 不可用（缺 greenlet），跳过 MySQLManager 单测")


class _UnknownDBError(Exception):
    """模拟 MySQL errno 1049（库不存在）。"""

    def __init__(self) -> None:
        super().__init__(1049, "Unknown database 'rag_ultra'")


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


def test_mysql_start_ok() -> None:
    MySQLManager, mod = _import_manager()
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(None)):
            asyncio.run(MySQLManager().start())
        assert app_state.db_engine is not None
    finally:
        app_state.db_engine = None


def test_mysql_start_fails_when_db_missing() -> None:
    MySQLManager, mod = _import_manager()
    from app.server import StartupError
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(_UnknownDBError())):
            with pytest.raises(StartupError):
                asyncio.run(MySQLManager().start())
        assert app_state.db_engine is None   # 失败后清理，不留半初始化引擎
    finally:
        app_state.db_engine = None


def test_mysql_start_fails_when_cannot_connect() -> None:
    MySQLManager, mod = _import_manager()
    from app.server import StartupError
    app_state.db_engine = None
    try:
        with mock.patch.object(mod, 'create_async_engine', _factory(RuntimeError("connect failed"))):
            with pytest.raises(StartupError):
                asyncio.run(MySQLManager().start())
        assert app_state.db_engine is None
    finally:
        app_state.db_engine = None


def test_is_unknown_database() -> None:
    _, mod = _import_manager()
    # 直接 DBAPI 异常：args[0]==1049
    assert mod._is_unknown_database(_UnknownDBError()) is True
    # 仅消息含 "Unknown database"
    assert mod._is_unknown_database(Exception("(1049, \"Unknown database 'x'\"))")) is True
    # SQLAlchemy 包裹形态：exc.orig.args[0]==1049
    wrapped = Exception("wrapper")
    wrapped.orig = _UnknownDBError()
    assert mod._is_unknown_database(wrapped) is True
    # 其它错误（连不上 / 1044 无权限）→ False
    assert mod._is_unknown_database(RuntimeError("connect failed")) is False
    assert mod._is_unknown_database(Exception("(1044, 'Access denied for user'))")) is False
