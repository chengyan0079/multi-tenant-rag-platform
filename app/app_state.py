"""
app_state - 全局共享状态与请求上下文

启动期注入：settings / redis / engine / milvus_store / model / file_provider / app。
请求期（contextvars）：tenant_id / user_id / trace_id，由 M1 依赖注入透传。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Optional

from common.config import Config, config
from common.logger import trace_id_ctx

if TYPE_CHECKING:
    from app.managers.nacos_discover_manager import NacosDiscoverManager
    from core.config_center.loader import ConfigCenterLoader
    from core.file_provider.provider import FileProvider
    from core.intent.classifier import QueryClassifier
    from core.rag_model.model_service import ModelService
    from fastapi import FastAPI
    from fastmcp import FastMCP
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine
    from store.milvus_store import MilvusStore


class _AppState:
    """进程级单例，各 Manager 在启动时把资源写到这里。"""

    def __init__(self) -> None:
        # 进程级资源：启动时由各 Manager 注入；未启动时均为 None。
        # 标注具体类型以提升可读性与类型检查（PEP 563，运行时零开销）。
        self.settings: Config = config                     # 全局配置（common.config 单例）
        self.redis: Optional[Redis] = None                 # redis.asyncio 客户端
        self.db_engine: Optional[AsyncEngine] = None          # SQLAlchemy 异步引擎
        self.milvus_store: Optional[MilvusStore] = None    # 向量库封装（含底层 client，经 .client 访问）
        self.model_service: Optional[ModelService] = None          # 模型服务单例
        self.intent_classifier: Optional[QueryClassifier] = None  # 意图分类单例（懒加载）
        self.file_provider: Optional[FileProvider] = None  # 文件提供者
        self.fastapi_app: Optional[FastAPI] = None                 # FastAPI app
        self.mcp_server: Optional[FastMCP] = None             # MCP 服务实例（MCPApiManager 注入）
        self.nacos_discovery: Optional[NacosDiscoverManager] = None  # 服务注册状态（供健康检查）
        self.config_center: Optional[ConfigCenterLoader] = None     # 配置中心长连接（NacosConfigManager 注入）


app_state = _AppState()

# 请求级上下文。contextvars 在 asyncio 多任务下天然隔离，不会串号。
tenant_id_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    'tenant_id', default=None)
user_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'user_id', default=None)
# trace_id_ctx 由 common.logger 提供（日志 patcher 需读取同一 ContextVar），此处再导出供中间件/依赖使用


def get_tenant_id() -> int | None:
    """返回当前请求的租户 id（未注入则为 None）。"""
    return tenant_id_ctx.get()


def get_trace_id() -> str | None:
    """返回当前请求的链路追踪 id（未注入则为 None）。"""
    return trace_id_ctx.get()
