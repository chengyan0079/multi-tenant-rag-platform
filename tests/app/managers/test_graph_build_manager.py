"""GraphBuildManager 单测：开关判定、租户遍历建图、优雅停止。

全部依赖用 mock 替换，不连真实 Neo4j / PostgreSQL / Nacos。
"""
import asyncio
from unittest import mock

import pytest

from app.app_state import app_state


@pytest.fixture
def mgr_env(monkeypatch):
    saved = {a: getattr(app_state, a) for a in ('db_engine', 'neo4j_store')}
    app_state.db_engine = object()
    app_state.neo4j_store = object()
    yield app_state
    for k, v in saved.items():
        setattr(app_state, k, v)


def test_disabled_does_not_start(mgr_env, monkeypatch):
    from app.managers.graph_build_manager import GraphBuildManager
    monkeypatch.setattr('app.managers.graph_build_manager.config.GRAPH_ENABLED', False)
    m = GraphBuildManager(interval_sec=1)
    asyncio.run(m.start())
    assert m._task is None


def test_schedule_disabled_does_not_start(mgr_env, monkeypatch):
    from app.managers.graph_build_manager import GraphBuildManager
    monkeypatch.setattr('app.managers.graph_build_manager.config.GRAPH_ENABLED', True)
    monkeypatch.setattr('app.managers.graph_build_manager.config.GRAPH_SCHEDULE_ENABLED', False)
    m = GraphBuildManager(interval_sec=1)
    asyncio.run(m.start())
    assert m._task is None


def test_tenant_ids_distinct(mgr_env, monkeypatch):
    from app.managers.graph_build_manager import GraphBuildManager

    class _Result:
        def all(self):
            return [(100,), (200,), (None,)]

    class _Session:
        async def execute(self, _stmt):
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr('app.managers.graph_build_manager.get_sessionmaker', lambda: _Ctx)
    m = GraphBuildManager(interval_sec=1)
    assert asyncio.run(m._tenant_ids()) == [100, 200]


def test_run_once_builds_all_tenants(mgr_env, monkeypatch):
    from app.managers.graph_build_manager import GraphBuildManager
    m = GraphBuildManager(interval_sec=1)
    monkeypatch.setattr(m, '_tenant_ids', mock.AsyncMock(return_value=[100, 200]))

    built = []
    async def fake_build(tid, store, session, engine, list_instances, nacos_client):
        built.append(tid)
        return {'nodes': 1, 'edges': 2}

    monkeypatch.setattr('offline.graph_build.build_graph', fake_build)
    monkeypatch.setattr('app.managers.graph_build_manager.async_sessionmaker', lambda *a, **k: _FakeSessionFactory())
    asyncio.run(m._run_once())
    assert built == [100, 200]


def test_stop_wakes_wait(mgr_env, monkeypatch):
    """stop 置位 Event，循环中的 wait 立即返回，不必等满周期。"""
    from app.managers.graph_build_manager import GraphBuildManager
    m = GraphBuildManager(interval_sec=9999)
    monkeypatch.setattr(m, '_run_once', mock.AsyncMock())
    monkeypatch.setattr(m, '_init_deps', mock.AsyncMock())
    monkeypatch.setattr(m, '_close_deps', mock.AsyncMock())
    asyncio.run(m.start())
    asyncio.run(m.stop())
    assert m._stop.is_set()
    assert m._task is not None and m._task.done()


class _FakeSession:
    async def __aenter__(self):
        return object()
    async def __aexit__(self, *a):
        return False


class _FakeSessionFactory:
    def __call__(self):
        return _FakeSession()
