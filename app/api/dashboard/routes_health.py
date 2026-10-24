# [运营后台 dashboard 接口]
"""健康 / 运维（§17.7）

探活聚合在 store.health_dao.HealthDao；本路由只做 HTTP 编排 + 响应组装。
"""
from fastapi import APIRouter

from app.api.schemas import HealthData
from common.response import ApiResponse, success
from store.health_dao import HealthDao

router = APIRouter(tags=["health"])


@router.get("/admin/health", response_model=ApiResponse[HealthData], summary="健康检查（探活）")
async def health():
    """健康检查（含 Milvus/PostgreSQL/Redis/Model/MCP/Nacos服务发现/配置中心），供 nacos 探活。

    除基础组件外，新增三项均取自内存状态、不做网络探测：
    MCP（是否已注册工具）、Nacos 服务发现（实例是否注册成功）、配置中心（长连接是否已连上）。
    未启用的能力为 False，不影响 status。
    """
    result = await HealthDao().check()
    return success(HealthData(status="ok", **result))
