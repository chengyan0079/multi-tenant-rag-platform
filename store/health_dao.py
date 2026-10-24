"""健康检查 DAO（store 数据访问层）：聚合 Redis / PostgreSQL / Milvus / Model / MCP / Nacos 探活。

返回原始探活结果（dict）；响应模型 HealthData 由路由层组装，故本模块不依赖 app.api.schemas。

探活原则：只读内存状态，不发网络请求——探活接口会被 Nacos 高频调用，
每次都打一次外部请求会把探活放大成压力；各组件"是否连上"由各自的 Manager 在
启动/运行时维护状态，这里只做汇总。
"""
from app.app_state import app_state
from store.postgres_store import ping as db_ping
from store.redis_store import ping as redis_ping


def _mcp_ok() -> bool:
    """MCP API 服务是否就绪：MCPApiManager 注册完工具后会把 FastMCP 实例写入 app_state。"""
    return app_state.mcp_server is not None


def _nacos_ok() -> bool:
    """Nacos 服务发现是否就绪：以 Manager 记录的注册结果为准（未启用时无 Manager）。"""
    mgr = app_state.nacos_discovery
    return mgr is not None and bool(getattr(mgr, 'is_registered', False))


def _config_center_ok() -> bool:
    """配置中心是否就绪：loader 是否已连上 Nacos（未启用时不会 start）。"""
    loader = app_state.config_center
    return loader is not None and bool(getattr(loader, 'is_started', False))


class HealthDao:
    async def check(self) -> dict:
        milvus_ok = False
        try:
            if app_state.milvus_store and app_state.milvus_store.client:
                milvus_ok = await app_state.milvus_store.client.has_collection(
                    app_state.settings.MILVUS_COLLECTION)
        except Exception:  # noqa: BLE001  探活失败不影响整体，置 False
            milvus_ok = False
        return {
            "redis": await redis_ping(),
            "postgres": await db_ping(),
            "milvus": bool(milvus_ok),
            "model": app_state.model_service is not None,
            "mcp": _mcp_ok(),
            "nacos": _nacos_ok(),
            "config_center": _config_center_ok(),
        }
