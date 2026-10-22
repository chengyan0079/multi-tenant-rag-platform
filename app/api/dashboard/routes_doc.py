# [运营后台 dashboard 接口]
"""文档（索引）（§17.4）

流程：POST /admin/docs（传上传得到的 file_id）→ 落 rag_document(status=0) →
投递索引任务（Redis 队列，由 app/managers/index_manager 消费）→ 后台完成切分 + 向量化建库；
GET /admin/docs/{doc_id}/status 轮询状态（pending/indexing/done/failed）。
数据访问在 store.doc_dao.DocDao / store.file_dao.FileDao；本路由只做 HTTP 编排 + 响应组装。
"""
from fastapi import APIRouter, Depends

from app.api.deps import resolve_tenant
from app.api.schemas import DocCreateResult, DocRequest, DocStatusData
from common.response import ApiResponse, ErrCode, fail, success
from core.rag_indexer.pipeline import enqueue_index
from store.doc_dao import DocDao
from store.file_dao import FileDao

router = APIRouter(prefix="/admin", tags=["doc"])

_STATUS_NAME = {0: "pending", 1: "indexing", 2: "done", 3: "failed"}


def _status_name(code: int) -> str:
    return _STATUS_NAME.get(code, "pending")


@router.post("/docs", response_model=ApiResponse[DocCreateResult], summary="创建文档索引")
async def create_doc(req: DocRequest, tenant_id: int = Depends(resolve_tenant)):
    """落文档记录 + 触发索引（异步消费）；返回 doc_id 供状态轮询。"""
    doc_id = await DocDao().create(tenant_id, req.file_id, req.doc_type)
    oss_key, file_name = await FileDao().get_oss_key(tenant_id, req.file_id)
    if not oss_key:
        await DocDao().update_status(doc_id, 3, err_msg="关联文件不存在")
        return success(DocCreateResult(tenant_id=tenant_id, doc_id=doc_id, status="failed"))
    await enqueue_index(tenant_id, doc_id, oss_key, file_name or "", req.doc_type)
    return success(DocCreateResult(tenant_id=tenant_id, doc_id=doc_id, status="pending"))


@router.get("/docs/{doc_id}/status", response_model=ApiResponse[DocStatusData], summary="查询文档索引状态")
async def doc_status(doc_id: int, tenant_id: int = Depends(resolve_tenant)):
    """查索引状态（pending/indexing/done/failed）。"""
    try:
        st = await DocDao().get_status(doc_id)
    except KeyError:
        return fail(ErrCode.NOT_FOUND, "doc not found")
    return success(DocStatusData(doc_id=doc_id, status=_status_name(st["status"])))
