"""
app_state - 全局共享状态与请求上下文

启动期注入：settings / redis / engine / milvus / model / file_provider / app。
请求期（contextvars）：tenant_id / user_id / trace_id，由 M1 依赖注入透传。
"""
import contextvars
from typing import Any

from common.config import config
from common.logger import trace_id_ctx


class _AppState:
    """进程级单例，各 Manager 在启动时把资源写到这里。"""

    def __init__(self) -> None:
        self.settings: Any = config
        self.redis: Any = None            # redis.asyncio 客户端
        self.engine: Any = None           # SQLAlchemy 异步引擎
        self.milvus: Any = None           # MilvusClient
        self.milvus_store: Any = None     # store.milvus_store.MilvusStore
        self.model: Any = None            # core.rag_model.model_service.ModelService
        self.intent_classifier: Any = None  # core.intent.QueryClassifier 单例，由 core.intent.get_classifier 懒加载提供
        self.file_provider: Any = None    # core.file_provider.provider.FileProvider
        self.app: Any = None              # FastAPI app
        self.mcp_api: Any = None          # FastMCP 实例（业务 API 的 MCP 服务，由 MCPApiManager 注入）
        self.nacos_discover: Any = None   # NacosDiscoverManager（服务注册状态，供健康检查读取）
        self.config_center: Any = None    # ConfigCenterLoader（配置中心长连接，由 NacosConfigManager 注入）


app_state = _AppState()

# 请求级上下文。contextvars 在 asyncio 多任务下天然隔离，不会串号。
tenant_id_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar('tenant_id', default=None)
user_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar('user_id', default=None)
# trace_id_ctx 由 common.logger 提供（日志 patcher 需读取同一 ContextVar），此处再导出供中间件/依赖使用


def get_tenant_id() -> int | None:
    return tenant_id_ctx.get()


def get_trace_id() -> str | None:
    return trace_id_ctx.get()
