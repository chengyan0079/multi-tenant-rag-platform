"""MCPApiManager - 启动项：把 app 的 HTTP API 封装成 MCP 工具，起 MCP 服务供外部调用。

启动时机：stage 3（路由已挂载、app_state.fastapi_app 就绪）。流程：
  1. 遍历 FastAPI 路由，为每个 API 生成一个 MCP 工具（排除文档 / 自描述 / 探活路径）；
  2. 注册到 FastMCP("rag-ultra-api")；
  3. 在**守护线程**起 MCP 服务（默认 streamable-http），外部客户端连
     http://{MCP_API_HOST}:{MCP_API_PORT}/mcp 即可调用业务 API。

两个关键设计：
- **工具调用走进程内 ASGI**（httpx ASGITransport）：不经网络、不额外占端口、不依赖服务已对外监听。
- **用独立线程而非 asyncio 任务**：MCP 服务内部会起 uvicorn，非主线程时 uvicorn 会自动跳过
  信号注册（capture_signals 仅主线程生效），避免与主服务的 SIGINT/SIGTERM 处理冲突。
"""
import re
import threading
from typing import Any, Iterator

from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from app.app_state import app_state
from app.server import Manager
from common.config import config
from common.logger import logger

_NON_WORD = re.compile(r'[^0-9a-zA-Z]+')
_SKIP_METHODS = frozenset({'HEAD', 'OPTIONS'})


def _tool_name(method: str, path: str) -> str:
    """路由转工具名：GET /admin/files/{oss_key} -> get_admin_files_oss_key。"""
    segs = [s.strip('{}').replace('-', '_') for s in path.strip('/').split('/') if s]
    raw = '_'.join([method.lower(), *segs])
    return _NON_WORD.sub('_', raw).strip('_')


def _excluded(path: str) -> bool:
    return any(path.startswith(p) for p in (config.MCP_API_EXCLUDE or []))


def _iter_routes(obj: Any, prefix: str = '') -> Iterator[tuple[Any, str]]:
    """展开路由树，产出 (APIRoute, 前缀)。

    FastAPI 0.143+ 的 include_router 生成的是惰性的 `_IncludedRouter`：它自身没有 methods，
    真实 APIRoute 在其 original_router 里。只遍历 app.routes 顶层会漏掉全部业务路由，
    所以这里递归下钻，并拼接 include 时的 prefix。
    """
    for route in getattr(obj, 'routes', []):
        if getattr(route, 'methods', None):
            yield route, prefix
            continue
        sub_prefix = prefix
        inc = getattr(route, 'include_context', None)
        if inc is not None:
            sub_prefix = prefix + (getattr(inc, 'prefix', '') or '')
        inner = getattr(route, 'original_router', None)
        if inner is not None:
            yield from _iter_routes(inner, sub_prefix)
        elif getattr(route, 'routes', None):
            yield from _iter_routes(route, sub_prefix)


def collect_routes(app: Any) -> list[dict[str, str]]:
    """收集 app 上需暴露的 HTTP API 路由（method + path + 工具名 + 摘要）。"""
    out: list[dict[str, str]] = []
    for route, prefix in _iter_routes(app):
        methods = getattr(route, 'methods', None) or []
        if not methods:
            continue                       # 非 APIRoute（如 Mount / WebSocket）
        path = prefix + getattr(route, 'path', '')
        if not path or _excluded(path):
            continue
        summary = (getattr(route, 'summary', '') or getattr(route, 'name', '')
                   or getattr(route, 'path', ''))
        for meth in sorted(methods):
            if meth in _SKIP_METHODS:
                continue
            out.append({
                'method': meth,
                'path': path,
                'name': _tool_name(meth, path),
                'summary': f'[{meth}] {path} - {summary}',
            })
    return out


def _make_tool(method: str, path: str):
    """生成一个 MCP 工具：以进程内 ASGI 调用对应 API，返回响应文本。"""

    async def call(
        path_params: dict | None = None,
        query: dict | None = None,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> str:
        """调用本服务 API。

        Args:
            path_params: 路径参数，如 {"oss_key": "a.pdf"}
            query: 查询参数，如 {"page": 1}
            body: JSON 请求体（POST/PUT/PATCH 用）
            headers: 附加请求头，如 {"X-Tenant-Id": "1", "X-User-Id": "u1"}
        """
        url = path
        for key, val in (path_params or {}).items():
            url = url.replace('{' + str(key) + '}', str(val))
        if '{' in url:
            raise ValueError(f'缺少路径参数: {url}（path_params={path_params}）')
        async with AsyncClient(
            transport=ASGITransport(app=app_state.fastapi_app), base_url='http://mcp-api',
        ) as client:
            resp = await client.request(
                method, url,
                params=query or None,
                json=body if body is not None else None,
                headers=headers or None,
            )
        return resp.text

    call.__name__ = _tool_name(method, path)
    return call


def build_mcp_api_server(app: Any) -> tuple[FastMCP, list[dict[str, str]]]:
    """按路由构建 MCP 服务并注册全部 API 工具。"""
    mcp = FastMCP('rag-ultra-api')
    routes = collect_routes(app)
    for info in routes:
        tool = _make_tool(info['method'], info['path'])
        tool.__doc__ = info['summary']
        mcp.tool()(tool)
    return mcp, routes


class MCPApiManager(Manager):
    """把业务 API 封装为 MCP 工具并在启动时加载进 MCP 服务。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if not config.MCP_API_ENABLED:
            logger.info('MCP API 服务未启用（MCP_API_ENABLED=False），跳过')
            return
        app = app_state.fastapi_app
        if app is None:
            logger.warning('FastAPI app 未就绪，跳过 MCP API 工具注册')
            return

        mcp, routes = build_mcp_api_server(app)
        app_state.mcp_server = mcp
        self._thread = threading.Thread(
            target=self._serve, args=(mcp,), name='mcp-api-server', daemon=True)
        self._thread.start()
        logger.info(
            f'MCP API 服务启动（{config.MCP_API_TRANSPORT} '
            f'{config.MCP_API_HOST}:{config.MCP_API_PORT}），已注册 {len(routes)} 个 API 工具')

    async def stop(self) -> None:
        # 服务在守护线程中随进程退出；此处仅清理引用
        app_state.mcp_server = None
        self._thread = None

    @staticmethod
    def _serve(mcp: FastMCP) -> None:
        """线程入口：阻塞运行 MCP 服务（非主线程，uvicorn 不抢信号）。"""
        try:
            mcp.run(transport=config.MCP_API_TRANSPORT,
                    host=config.MCP_API_HOST, port=config.MCP_API_PORT)
        except Exception:  # noqa: BLE001
            logger.exception('MCP API 服务线程异常退出')
