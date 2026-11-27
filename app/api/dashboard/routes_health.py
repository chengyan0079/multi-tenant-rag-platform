"""健康检查接口（探活，供 Nacos 调用）。"""
from fastapi import APIRouter

from app.api.schemas import HealthData
from common.response import ApiResponse, success
from store.health_dao import HealthDao

router = APIRouter(tags=["health"])


@router.get("/admin/health", response_model=ApiResponse[HealthData], summary="健康检查（探活）")
async def health():
    """健康检查（含 Milvus/PostgreSQL/Redis/Model/MCP/Nacos/配置中心/故障图谱），供 nacos 探活。

    Redis/PostgreSQL/Milvus 做真实探测；其余组件取 Manager 维护的状态。
    未启用的能力为 False，不影响 status。
    """
    result = await HealthDao().check()
    return success(HealthData(status="ok", **result))
