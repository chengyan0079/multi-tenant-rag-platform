"""MCPApiManager 单测：路由收集 / 工具命名 / 排除、进程内 ASGI 调用、启动编排（线程用假对象）。"""
import asyncio
from unittest import mock

from fastapi import APIRouter, FastAPI

from app.app_state import app_state
from app.managers import mcp_api_manager as m
from common.config import config


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get('/admin/health')
    async def health():
        return {'ok': True}

    @app.get('/admin/files/{oss_key}')
    async def get_file(oss_key: str):
        return {'key': oss_key}

    @app.post('/echo')
    async def echo(payload: dict):
        return {'got': payload}

    return app


def test_collect_routes_names_and_excludes(monkeypatch) -> None:
    monkeypatch.setattr(config, 'MCP_API_EXCLUDE', ['/admin/health'])
    routes = m.collect_routes(_make_app())
    pairs = {(r['method'], r['name']) for r in routes}
    assert ('GET', 'get_admin_files_oss_key') in pairs
    assert ('POST', 'post_echo') in pairs
    assert all(r['path'] != '/admin/health' for r in routes)   # 排除项不暴露


def test_collect_routes_expands_included_routers() -> None:
    """业务路由都是 include_router 挂进来的（FastAPI 0.143+ 为惰性 _IncludedRouter），
    必须递归下钻，否则一个工具都收集不到。"""
    app = FastAPI()
    sub = APIRouter(prefix='/admin')

    @sub.get('/config/list')
    async def list_config():
        return {'items': []}

    app.include_router(sub)
    routes = m.collect_routes(app)
    pairs = {(r['method'], r['path']) for r in routes}
    assert ('GET', '/admin/config/list') in pairs     # 前缀正确拼接


def test_tool_calls_api_in_process() -> None:
    """工具以进程内 ASGI 调用 API（不经网络）：路径参数 / 请求体均生效。"""
    app_state.app = _make_app()
    try:
        assert asyncio.run(m._make_tool('GET', '/admin/files/{oss_key}')(
            path_params={'oss_key': 'a.pdf'})) == '{"key":"a.pdf"}'
        assert asyncio.run(m._make_tool('POST', '/echo')(body={'x': 1})) == '{"got":{"x":1}}'
    finally:
        app_state.app = None


def test_start_disabled_skips(monkeypatch) -> None:
    monkeypatch.setattr(config, 'MCP_API_ENABLED', False)
    app_state.app = _make_app()
    try:
        with mock.patch.object(m.threading, 'Thread') as th:
            asyncio.run(m.MCPApiManager().start())
        th.assert_not_called()
    finally:
        app_state.app = None


def test_start_registers_tools_and_spawns_thread(monkeypatch) -> None:
    app_state.app = _make_app()
    captured: dict = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), **kw):
            captured['target'] = target
            captured['args'] = args

        def start(self):
            captured['started'] = True

    monkeypatch.setattr(config, 'MCP_API_ENABLED', True)
    try:
        with mock.patch.object(m.threading, 'Thread', _FakeThread):
            asyncio.run(m.MCPApiManager().start())
        assert captured.get('started') is True
        assert app_state.mcp_api is not None      # MCP 服务已构建并注入
    finally:
        app_state.app = None
        app_state.mcp_api = None
