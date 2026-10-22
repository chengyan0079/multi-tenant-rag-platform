"""
api - FastAPI 路由层（M1 接入）

按调用方做包隔离，防止用户侧与运营后台接口混淆：
- app.api.user      ：终端用户侧接口（问答 / 会话）
- app.api.dashboard ：运营后台接口（文档 / FAQ / 文件 / 管理台 / 配置中心 / 检索调试 / 健康）

共享依赖（deps）、异常处理器（exceptions）、请求/响应契约（schemas）保持在 app/api 根，被两侧复用。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.config import config


def add_cors(app: FastAPI) -> None:
    """按配置挂载 CORS 中间件（跨域白名单来源 config.CORS_ALLOW_ORIGINS）。

    默认白名单为空即不放行任何跨域源；生产在 .env 配置 CORS_ALLOW_ORIGINS。
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ALLOW_ORIGINS,
        allow_methods=config.CORS_ALLOW_METHODS,
        allow_headers=config.CORS_ALLOW_HEADERS,
        allow_credentials=config.CORS_ALLOW_CREDENTIALS,
    )


def configure_routes(app: FastAPI) -> None:
    """统一挂载所有路由到 FastAPI 应用，并注册 trace 中间件、CORS 与全局异常处理器。

    路由在函数内惰性导入：避免仅 import app.api（例如取 app.api.schemas 契约）时，
    连带拉起全部路由的重依赖（sqlalchemy / pymilvus 等），保持包导入轻量。
    """
    from app.api.user import routes_qa, routes_session
    from app.api.dashboard import (
        routes_health, routes_doc, routes_faq, routes_file,
        routes_config_read, routes_config_write, routes_retrieve,
    )

    app.include_router(routes_health.router)
    app.include_router(routes_qa.router)
    app.include_router(routes_session.router)
    app.include_router(routes_file.router)
    app.include_router(routes_doc.router)
    app.include_router(routes_faq.router)
    app.include_router(routes_config_read.router)
    app.include_router(routes_config_write.router)
    app.include_router(routes_retrieve.router)

    from app.middleware import ApiLogMiddleware, AuthSignatureMiddleware, TraceMiddleware
    # 后加者位于更外层，故实际执行顺序（外→内）为：CORS → Trace → ApiLog → Auth → 路由。
    # Auth 放最内层：验签失败时 Trace/ApiLog 仍会记录这次请求，便于审计被拒调用。
    app.add_middleware(AuthSignatureMiddleware)
    app.add_middleware(ApiLogMiddleware)
    app.add_middleware(TraceMiddleware)
    # CORS 最后加 → 位于更外层，保证跨域头覆盖含错误响应在内的所有返回
    add_cors(app)

    from app.api.exceptions import register_exception_handlers
    register_exception_handlers(app)
