"""
app_factory - FastAPI 应用工厂（uvicorn factory 模式，多进程安全）

每个 worker 进程各自调用 create_app()：构建 AppServer、按 stage 注册各启动项 Manager、
挂载路由，并在 lifespan 内按阶段启动 / 逆序释放这些 Manager（任一失败即记 "启动失败" 日志）。

启动分 stage（同 stage 内 asyncio.gather 并发，stage 间按序）：
  stage 0：Logging（奠基）
  stage 1：Redis / PostgreSQL / Model / FileProvider（相互独立，并发）
  stage 2：Milvus(依赖 Model) / FaqCache(依赖 Redis+PostgreSQL) / NacosConfig(依赖 Redis)
  stage 3：NacosDiscover（服务注册，最后）

配置为 common.config 单例，已由 app_state.settings 暴露，无需单独 ConfigManager。
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.app_state import app_state
from app.server import AppServer, StartupError
from app.api import configure_routes
from common.config import config
from common.logger import logger

from app.managers.logging_manager import LoggingManager
from app.managers.redis_manager import RedisManager
from app.managers.postgres_manager import PostgresManager
from app.managers.faq_cache_manager import FaqCacheManager
from app.managers.milvus_manager import MilvusManager
from app.managers.model_manager import ModelManager
from app.managers.file_provider_manager import FileProviderManager
from app.managers.nacos_config_manager import NacosConfigManager
from app.managers.nacos_discover_manager import NacosDiscoverManager
from app.managers.mcp_api_manager import MCPApiManager
from app.managers.index_manager import IndexManager


def build_server() -> AppServer:
    """按 stage 注册全部启动项（stage 内并发、stage 间按序）。"""
    server = AppServer("rag-ultra")
    server.register(LoggingManager(), order=10, stage=0)            # 奠基：日志
    server.register(RedisManager(), order=20, stage=1)              # 基础组件
    server.register(PostgresManager(), order=30, stage=1)            # 基础组件
    server.register(ModelManager(), order=50, stage=1)              # 基础组件
    server.register(MilvusManager(), order=55, stage=2)             # 依赖 Model（stage 在其后）
    server.register(FileProviderManager(), order=60, stage=1)       # 基础组件
    server.register(FaqCacheManager(), order=65, stage=2)           # 依赖 Redis+PostgreSQL
    server.register(NacosConfigManager(), order=105, stage=2)       # 依赖 Redis
    server.register(NacosDiscoverManager(), order=110, stage=3)     # 服务注册
    server.register(IndexManager(), order=115, stage=3)             # 后台消费（仅起协程，不阻塞）
    server.register(MCPApiManager(), order=120, stage=3)            # 需路由已挂载，最后
    return server


def _build_fastapi() -> FastAPI:
    kwargs: dict = dict(
        title=config.SERVICE_NAME,
        version=config.VERSION,
        description="FAQ+RAG智慧问答服务",
    )
    if not config.OPENAPI_ENABLED:
        kwargs["openapi_url"] = None
        kwargs["docs_url"] = None
        kwargs["redoc_url"] = None
    return FastAPI(**kwargs)


def create_app() -> FastAPI:
    """uvicorn factory 入口：每个 worker 进程调用一次，独立初始化各自资源。"""
    server = build_server()
    app = _build_fastapi()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info('[rag-ultra] 启动流程开始')
        try:
            await server.start_all()
        except StartupError:
            logger.exception('[rag-ultra] 启动失败，进程退出')
            raise
        logger.info('[rag-ultra] 启动完成，开始提供服务')
        yield
        logger.info('[rag-ultra] 收到关闭信号，开始释放资源')
        await server.stop_all()
        logger.info('[rag-ultra] 资源释放完成')

    app.router.lifespan_context = lifespan
    configure_routes(app)
    app_state.fastapi_app = app
    return app
