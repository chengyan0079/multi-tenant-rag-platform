"""FAQ 高频问题管理接口（运营后台）。命中由问答主流程 store.faq_cache 做精确匹配。"""
from fastapi import APIRouter, Depends

from app.api.deps import resolve_tenant
from app.api.schemas import (
    FaqCreateResult, FaqDeleteResult, FaqItem, FaqRequest, FaqUpdateResult,
)
from common.response import ApiResponse, ErrCode, fail, success
from store.faq_dao import FaqDao

router = APIRouter(prefix="/admin", tags=["faq"])


def _to_item(row) -> FaqItem:
    """ORM 行 → 响应模型。"""
    return FaqItem(
        faq_id=row.id, tenant_id=row.tenant_id, question=row.question, answer=row.answer,
        question_type=row.question_type, similar_questions=row.similar_questions,
        enabled=row.enabled, updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.post("/faq", response_model=ApiResponse[FaqCreateResult], summary="新建 FAQ")
async def create_faq(req: FaqRequest, tenant_id: int = Depends(resolve_tenant)):
    """新建 FAQ（落数据库 + 写穿透刷新 Redis 缓存）。"""
    faq_id = await FaqDao().create(
        tenant_id, req.question, req.answer, req.question_type, req.similar_questions)
    return success(FaqCreateResult(faq_id=faq_id, tenant_id=tenant_id, status="created"))


@router.put("/faq/{faq_id}", response_model=ApiResponse[FaqUpdateResult], summary="更新 FAQ")
async def update_faq(faq_id: int, req: FaqRequest, tenant_id: int = Depends(resolve_tenant)):
    """更新 FAQ（按租户隔离）。"""
    ok = await FaqDao().update(
        tenant_id, faq_id, req.question, req.answer, req.question_type, req.similar_questions)
    if not ok:
        return fail(ErrCode.NOT_FOUND, "faq not found")
    return success(FaqUpdateResult(faq_id=faq_id, status="updated"))


@router.delete("/faq/{faq_id}", response_model=ApiResponse[FaqDeleteResult], summary="删除 FAQ（软删除）")
async def delete_faq(faq_id: int, tenant_id: int = Depends(resolve_tenant)):
    """删除 FAQ（按租户隔离，软删除：enabled=0）。"""
    if not await FaqDao().soft_delete(tenant_id, faq_id):
        return fail(ErrCode.NOT_FOUND, "faq not found")
    return success(FaqDeleteResult(faq_id=faq_id, status="deleted"))


@router.get("/faq/{faq_id}", response_model=ApiResponse[FaqItem], summary="查看单条 FAQ")
async def get_faq(faq_id: int, tenant_id: int = Depends(resolve_tenant)):
    """按 id 查看单条 FAQ（按租户隔离）。"""
    row = await FaqDao().get(tenant_id, faq_id)
    if row is None:
        return fail(ErrCode.NOT_FOUND, "faq not found")
    return success(_to_item(row))


@router.get("/faq", response_model=ApiResponse[list[FaqItem]], summary="FAQ 列表")
async def list_faq(tenant_id: int = Depends(resolve_tenant)):
    """列出本租户启用中的 FAQ。"""
    rows = await FaqDao().list(tenant_id)
    return success([_to_item(r) for r in rows])
