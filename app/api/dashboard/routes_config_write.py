# [运营后台 dashboard 接口]
"""配置中心变更接口（审批流，不直接写 Nacos）。

变更须经 申请 → 复核 →（后端 ConfigChangeService 代理）执行 Nacos 写。
查询类接口见 routes_config_read。请求体 / 响应 data 结构统一见 app.api.schemas。
"""
from fastapi import APIRouter, Depends

from app.api.deps import resolve_tenant
from app.api.schemas import (
    ConfigRequestItem, ConfigRequestListData, CreateRequestIn,
    IdStatus, ReviewIn, RollbackIn, SyncHistoryResult,
)
from common.response import ApiResponse, success
from core.config_center import service
from store.orm_models.config import ConfigChangeRequest
from store.postgres_store import get_session

router = APIRouter(
    prefix="/admin/config",
    tags=["config-center-write"],
    dependencies=[Depends(resolve_tenant)],
)


def _to_item(r: ConfigChangeRequest) -> ConfigRequestItem:
    """ORM 行 → 响应模型。"""
    return ConfigRequestItem(
        id=r.id, namespace=r.namespace, group_id=r.group_id, data_id=r.data_id,
        domain=r.domain, op_type=r.op_type, title=r.title, content_format=r.content_format,
        status=r.status, applicant=r.applicant, reviewer=r.reviewer,
        reason=r.reason, created_at=r.created_at, updated_at=r.updated_at,
    )


@router.post("/requests", response_model=ApiResponse[IdStatus], summary="提交配置变更申请")
async def create_req(body: CreateRequestIn, session=Depends(get_session)):
    """提交配置变更申请（新增 / 修改 / 删除）。"""
    req = await service.create_request(
        session, namespace=body.namespace, group_id=body.group_id, data_id=body.data_id,
        domain=body.domain, op_type=body.op_type, title=body.title,
        content_new=body.content_new, content_format=body.content_format, reason=body.reason,
    )
    return success(IdStatus(id=req.id, status=req.status))


@router.get("/requests", response_model=ApiResponse[ConfigRequestListData], summary="变更申请列表")
async def list_req(
    status: int | None = None,    # 按状态筛选（可选）
    domain: str | None = None,    # 按业务域筛选（可选）
    page: int = 1,                # 页码，从 1 开始
    page_size: int = 20,          # 每页条数
    session=Depends(get_session),
):
    """变更申请列表，可按状态 / 域筛选。"""
    items, total = await service.list_requests(
        session, status=status, domain=domain, page=page, page_size=page_size)
    return success(ConfigRequestListData(items=[_to_item(r) for r in items], total=total))


@router.get("/requests/{request_id}", summary="变更申请详情")
async def req_detail(request_id: int, session=Depends(get_session)):
    """申请详情，含变更前后内容，供复核时对比（结构由 service 动态组装，透传）。"""
    data = await service.get_request_detail(session, request_id=request_id)
    return success(data)


@router.post("/requests/{request_id}/review", response_model=ApiResponse[IdStatus], summary="复核变更申请")
async def do_review(request_id: int, body: ReviewIn, session=Depends(get_session)):
    """复核申请：通过（置为待执行）或驳回。通过后需再调 execute 真正写 Nacos。"""
    req = await service.review(session, request_id=request_id, action=body.action, comment=body.comment)
    return success(IdStatus(id=req.id, status=req.status))


@router.post("/requests/{request_id}/execute", response_model=ApiResponse[IdStatus], summary="执行变更申请（写 Nacos）")
async def do_execute(request_id: int, session=Depends(get_session)):
    """执行已通过的申请：真正写入 Nacos（带 CAS）。"""
    req = await service.execute_request(session, request_id=request_id)
    return success(IdStatus(id=req.id, status=req.status))


@router.post("/rollback", response_model=ApiResponse[IdStatus], summary="提交回滚申请")
async def do_rollback(body: RollbackIn, session=Depends(get_session)):
    """提交回滚申请，回滚到指定历史版本。"""
    req = await service.create_rollback(
        session, namespace=body.namespace, group_id=body.group_id, data_id=body.data_id,
        domain=body.domain, src_history_nid=body.src_history_nid, title=body.title, reason=body.reason,
    )
    return success(IdStatus(id=req.id, status=req.status))


@router.post("/requests/{request_id}/sync-history", response_model=ApiResponse[SyncHistoryResult], summary="补偿同步历史 nid")
async def do_sync_history(request_id: int, session=Depends(get_session)):
    """程序对齐：重新从 Nacos 拉历史，回填 old/nacos history nid（供自愈或补偿调用，无需人工）。"""
    req = await service.sync_history_nids(session, request_id=request_id)
    return success(SyncHistoryResult(
        id=req.id, status=req.status,
        nacos_history_nid=req.nacos_history_nid,
        old_history_nid=req.old_history_nid,
    ))
