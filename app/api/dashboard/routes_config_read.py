# [运营后台 dashboard 接口]
"""配置中心只读接口（直连 Nacos，无副作用）。

仅查询：命名空间列表 / 配置列表 / 配置详情 / 历史列表 / 历史详情。
所有写操作见 routes_config_write（走审批流，不直接写 Nacos）。
Nacos 原始结构（namespaces/list/history）属外部数据，按原样透传；仅 detail 的
{'content': ...} 收敛为 app.api.schemas.ConfigContentData。
"""
import asyncio

from fastapi import APIRouter, Depends
from app.api.deps import resolve_tenant
from app.api.schemas import ConfigContentData
from common.response import ApiResponse, BizException, success
from core.config_center.nacos_config import ConfigCenterError, get_config_center_client
from core.config_center.query import get_detail, list_by_group

router = APIRouter(
    prefix="/admin/config",
    tags=["config-center-read"],
    dependencies=[Depends(resolve_tenant)],
)


async def _nacos(fn, *args, **kwargs):
    """把 Nacos 同步 SDK 调用放进线程池，避免阻塞事件循环；异常转 BizException。"""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ConfigCenterError as e:
        raise BizException(50200, f'Nacos 查询失败: {e}')


@router.get("/namespaces", summary="Nacos 命名空间列表")
async def namespaces():
    """Nacos 命名空间列表。"""
    return success(await _nacos(get_config_center_client().list_namespaces))


@router.get("/list", summary="配置列表")
async def list_cfg(
    data_id: str | None = None,   # 按 data_id 过滤（可选）
    group: str | None = None,     # 配置分组（可选）
    page_no: int = 1,             # 页码，从 1 开始
    page_size: int = 100,         # 每页条数
):
    """按 group 列出配置（可再按 data_id 过滤）。底层 Nacos /nacos/v1/cs/configs。"""
    return success(await _nacos(list_by_group, group, data_id, page_no, page_size))


@router.get("/detail", response_model=ApiResponse[ConfigContentData], summary="配置详情（当前内容）")
async def detail(
    data_id: str,                # 配置 data_id（必填）
    group: str | None = None,    # 配置分组（可选）
):
    """读取某 group + data_id 的当前配置内容。"""
    return success(ConfigContentData(content=await _nacos(get_detail, group, data_id)))


@router.get("/history", summary="配置历史版本列表")
async def history(
    data_id: str,                # 配置 data_id（必填）
    group: str | None = None,    # 配置分组（可选）
    page_no: int = 1,            # 页码，从 1 开始
    page_size: int = 100,        # 每页条数
):
    """列出某配置的历史版本。"""
    return success(await _nacos(get_config_center_client().list_history, data_id, group, page_no, page_size))


@router.get("/history/{nid}", summary="配置历史版本详情")
async def history_detail(
    data_id: str,                # 配置 data_id（必填）
    nid: int,                    # 历史版本 nid
    group: str | None = None,    # 配置分组（可选）
):
    """查看某个历史版本的配置内容。"""
    return success(await _nacos(get_config_center_client().get_history, data_id, nid, group))
