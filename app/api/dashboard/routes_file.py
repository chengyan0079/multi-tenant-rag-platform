# [运营后台 dashboard 接口]
"""文件（文档上传 / 版本 / 下载）（§17.3）

数据模型：rag_file（文档列表，一个 base_key 一行）+ rag_file_version（版本历史，一个版本一行）。
上传幂等：同租户同 base_key 复用同一文档；同文档同内容（file_hash）不新增版本（dedup）；
版本号 MAX(version)+1（撞唯一约束重试）；对象路径含版本，互不覆盖。
数据访问在 store.file_dao.FileDao；本路由只做 HTTP 编排 + 实体→响应映射（上传人校验、扩展名解析）。
"""
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File as FileParam, Form, HTTPException, Query, Response, UploadFile,
)

from app.api.deps import resolve_tenant
from app.api.schemas import (
    DocumentListData, DocumentListItem, FileBatchItem, FileBatchUploadItem,
    FileUploadResult, FileVersionItem, FileVersionsData, OkData,
)
from app.app_state import user_id_ctx
from common.response import ApiResponse, success
from store.file_dao import FileDao

router = APIRouter(prefix="/admin", tags=["file"])

_MIME = {
    'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg', 'gif': 'image/gif', 'txt': 'text/plain',
    'json': 'application/json', 'csv': 'text/csv', 'md': 'text/markdown',
    'html': 'text/html', 'xml': 'application/xml', 'zip': 'application/zip',
}


def _media_type(file_name: str) -> str:
    ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
    return _MIME.get(ext, 'application/octet-stream')


def _ext(filename: str | None) -> str:
    return filename.rsplit('.', 1)[-1] if (filename and '.' in filename) else 'bin'


def _require_upload_user() -> str:
    """上传需记录上传人：要求网关下发 X-User-Id（resolve_tenant 已写入 user_id_ctx）。"""
    uid = user_id_ctx.get()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 X-User-Id 请求头（上传需记录上传人）")
    return uid


def _doc_item(r) -> DocumentListItem:
    return DocumentListItem(
        base_key=r.base_key, file_name=r.file_name, file_type=r.file_type,
        create_user_id=r.create_user_id, current_version=r.current_version,
        current_file_size=r.current_file_size, version_count=r.version_count,
        status=r.status, updated_at=r.updated_at)


def _ver_item(r) -> FileVersionItem:
    return FileVersionItem(
        version=r.version, oss_key=r.oss_key, file_name=r.file_name, file_type=r.file_type,
        file_size=r.file_size, file_hash=r.file_hash, upload_user_id=r.upload_user_id,
        status=r.status, created_at=r.created_at.isoformat() if r.created_at else None)


@router.post(
    "/files/upload",
    response_model=ApiResponse[FileUploadResult],
    summary="上传文件（单发，幂等）",
)
async def upload_file(
    file: UploadFile = FileParam(...),
    base_key: str = Query(..., description="文档业务键（同文档多版本共用）"),
    tenant_id: int = Depends(resolve_tenant),
):
    """上传：get-or-create 文档 + 内容去重 + 分配版本 + 存对象 + 写版本历史。"""
    upload_user = _require_upload_user()
    out = await FileDao().save_upload(
        tenant_id, base_key, await file.read(), _ext(file.filename), upload_user)
    return success(FileUploadResult(
        base_key=base_key, file_id=out.file_id, version=out.version,
        oss_key=out.oss_key, dedup=out.dedup, expires_at=out.expires_at))


@router.post(
    "/files/batch-upload",
    response_model=ApiResponse[list[FileBatchUploadItem]],
    summary="批量上传文件",
)
async def batch_upload_files(
    files: list[UploadFile] = FileParam(...),
    base_keys: list[str] = Form(default=[]),
    tenant_id: int = Depends(resolve_tenant),
):
    """批量上传：多文件 + 可选 base_keys，逐个走幂等上传流程。"""
    upload_user = _require_upload_user()
    dao = FileDao()
    results: list[FileBatchUploadItem] = []
    for idx, uf in enumerate(files):
        base_key = base_keys[idx] if idx < len(base_keys) else (uf.filename or 'file')
        out = await dao.save_upload(tenant_id, base_key, await uf.read(), _ext(uf.filename), upload_user)
        results.append(FileBatchUploadItem(
            base_key=base_key, file_id=out.file_id, version=out.version,
            oss_key=out.oss_key, dedup=out.dedup, expires_at=out.expires_at))
    return success(results)


@router.get("/files", response_model=ApiResponse[DocumentListData], summary="文档列表")
async def list_documents(tenant_id: int = Depends(resolve_tenant)):
    """文档列表：一个 base_key 一行（含当前版本 + 版本计数），按更新时间倒序。"""
    rows = await FileDao().list_documents(tenant_id)
    return success(DocumentListData(items=[_doc_item(r) for r in rows], total=len(rows)))


@router.get(
    "/files/{base_key}/versions",
    response_model=ApiResponse[FileVersionsData],
    summary="文档版本历史",
)
async def file_versions(base_key: str, tenant_id: int = Depends(resolve_tenant)):
    """版本历史：某文档的各版本（含上传人/大小/哈希/oss_key），按版本倒序。"""
    rows = await FileDao().list_versions(tenant_id, base_key)
    return success(FileVersionsData(base_key=base_key, versions=[_ver_item(r) for r in rows]))


@router.get("/files/{oss_key:path}", summary="下载文件（原始字节流）")
async def get_file(
    oss_key: str,
    tenant_id: int = Depends(resolve_tenant),
):
    """按 oss_key 下载原始字节（带租户归属校验，防跨租户下载）；下载勿用 base64。"""
    try:
        content = await FileDao().get_bytes(oss_key, tenant_id)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="文件不存在或已过期")
    file_name = oss_key.rstrip('/').split('/')[-1] or 'file'
    return Response(
        content,
        media_type=_media_type(file_name),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"},
    )


@router.post(
    "/files/batch",
    response_model=ApiResponse[list[FileBatchItem]],
    summary="批量下载文件（base64，小文件）",
)
async def batch_files(
    oss_keys: list[str],
    base64: bool = True,
    tenant_id: int = Depends(resolve_tenant),
):
    """批量下载：逐个带租户归属校验；JSON 内以 base64 承载字节（仅小文件，超限返回空 base64）。"""
    refs = await FileDao().batch_get_files(oss_keys, base64, tenant_id)
    return success([FileBatchItem(oss_key=r.oss_key, base64=r.base64) for r in refs])


@router.delete("/files/{base_key}", response_model=ApiResponse[OkData], summary="删除文档（软删除）")
async def delete_document(base_key: str, tenant_id: int = Depends(resolve_tenant)):
    """删除文档（软删除 status=0；幂等，版本历史保留）。"""
    if not await FileDao().delete_document(tenant_id, base_key):
        raise HTTPException(status_code=404, detail="文档不存在")
    return success(OkData())
