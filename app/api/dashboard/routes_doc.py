# [运营后台 dashboard 接口]
"""文档（索引）（§17.4）

数据访问在 store.doc_dao.DocDao（占位）；本路由只做 HTTP 编排 + 响应组装。
"""
from fastapi import APIRouter, Depends

from app.api.deps import resolve_tenant
from app.api.schemas import DocCreateResult, DocRequest, DocStatusData
from common.response import ApiResponse, success
from store.doc_dao import DocDao

router = APIRouter(prefix="/admin", tags=["doc"])


@router.post("/docs", response_model=ApiResponse[DocCreateResult], summary="创建文档索引（占位）")
async def create_doc(req: DocRequest, tenant_id: int = Depends(resolve_tenant)):
    """建 doc 记录 + 触发索引（异步 Worker 投递占位）。"""
    status = await DocDao().create(tenant_id, req.oss_key)
    return success(DocCreateResult(tenant_id=tenant_id, oss_key=req.oss_key, status=status))


@router.get("/docs/{doc_id}/status", response_model=ApiResponse[DocStatusData], summary="查询文档索引状态")
async def doc_status(doc_id: int, tenant_id: int = Depends(resolve_tenant)):
    """查索引任务状态（脚手架占位）。"""
    status = await DocDao().get_status(doc_id)
    return success(DocStatusData(doc_id=doc_id, status=status))
