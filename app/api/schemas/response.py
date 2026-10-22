"""API 响应 data 模型（接入层 M1）——与请求模型（request.py）分离维护。

路由 return success(<模型实例>) 时，data 的具体结构即本模块模型；配合
common.response.ApiResponse[T] 泛型信封做序列化/校验。字段附中文注释（description），
便于 OpenAPI 文档与前后端对齐。响应以描述为主，避免对输出值做强约束以防误拒。
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ============ 通用 ============
class OkData(BaseModel):
    ok: bool = Field(True, description="是否成功")


class IdStatus(BaseModel):
    id: int = Field(..., description="资源 id")
    status: int = Field(..., description="状态码")


# ============ 问答 / 会话（用户侧）============
class QAChunk(BaseModel):
    model_config = ConfigDict(extra='allow')
    content: str = Field('', description="片段内容")
    doc_name: str | None = Field(None, description="来源文档名")
    page: int | None = Field(None, description="页码")
    score: float | None = Field(None, description="相关度分数")


class QAResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    answer: str = Field('', description="答案文本")
    source: str = Field('empty', description="来源：cache / faq / rag / empty")
    intent: str = Field('', description="识别到的意图")
    session_id: str | None = Field(None, description="会话 id")
    chunks: list[QAChunk] = Field(default_factory=list, description="召回片段")


class SessionCreated(BaseModel):
    session_id: str = Field(..., description="服务端生成的会话 id")
    title: str = Field('', description="会话标题")


class SessionItem(BaseModel):
    session_id: str = Field(..., description="会话 id")
    title: str = Field('', description="标题")
    preview: str = Field('', description="最近一条预览")
    memory_summary: str = Field('', description="记忆摘要")
    turn_count: int = Field(0, description="对话轮数")
    created_at: Any = Field(None, description="创建时间")
    updated_at: Any = Field(None, description="更新时间")


class SessionListData(BaseModel):
    sessions: list[SessionItem] = Field(default_factory=list, description="会话列表")


class SessionDetail(BaseModel):
    model_config = ConfigDict(extra='allow')
    session_id: str = Field(..., description="会话 id")
    title: str = Field('', description="标题")
    content: list[dict[str, Any]] = Field(default_factory=list, description="对话原文（role/content）")
    memory: dict[str, Any] = Field(default_factory=dict, description="会话记忆（summary/facts/updated_at）")
    created_at: Any = Field(None, description="创建时间")
    updated_at: Any = Field(None, description="更新时间")


# ============ FAQ（运营后台）============
class FaqItem(BaseModel):
    faq_id: int = Field(..., description="FAQ id")
    tenant_id: int = Field(..., description="租户 id")
    question: str = Field(..., description="标准问题")
    answer: str = Field(..., description="标准答案")
    question_type: int = Field(0, description="问题类型：0=通用知识 1=专业咨询")
    similar_questions: str | None = Field(None, description="相似问题（多行文本）")
    enabled: int = Field(1, description="是否启用：1=启用 0=停用")
    updated_at: str | None = Field(None, description="更新时间（ISO）")


class FaqCreateResult(BaseModel):
    faq_id: int = Field(..., description="新建 FAQ id")
    tenant_id: int = Field(..., description="租户 id")
    status: str = Field(..., description="状态")


class FaqUpdateResult(BaseModel):
    faq_id: int = Field(..., description="FAQ id")
    status: str = Field(..., description="状态")


class FaqDeleteResult(BaseModel):
    faq_id: int = Field(..., description="FAQ id")
    status: str = Field(..., description="状态")


# ============ 文件（运营后台）============
class FileUploadResult(BaseModel):
    base_key: str = Field(..., description="文档业务键")
    file_id: int = Field(..., description="文档ID（rag_file.id）")
    version: int = Field(..., description="版本号")
    oss_key: str = Field(..., description="对象存储 key")
    dedup: bool = Field(False, description="是否命中内容去重（未新增版本）")
    expires_at: str | None = Field(None, description="过期时间（ISO）")


class FileBatchUploadItem(BaseModel):
    base_key: str = Field(..., description="文档业务键")
    file_id: int = Field(..., description="文档ID")
    version: int = Field(..., description="版本号")
    oss_key: str = Field(..., description="对象存储 key")
    dedup: bool = Field(False, description="是否命中内容去重")
    expires_at: str | None = Field(None, description="过期时间（ISO）")


class DocumentListItem(BaseModel):
    base_key: str = Field(..., description="文档业务键")
    file_name: str | None = Field(None, description="当前版本文件名")
    file_type: str | None = Field(None, description="当前版本文件类型")
    create_user_id: str = Field(..., description="创建人")
    current_version: int = Field(0, description="当前版本号")
    current_file_size: int | None = Field(None, description="当前版本大小（字节）")
    version_count: int = Field(0, description="版本总数")
    status: int = Field(1, description="状态（1=有效 0=删除）")
    updated_at: Any = Field(None, description="更新时间")


class DocumentListData(BaseModel):
    items: list[DocumentListItem] = Field(default_factory=list, description="文档列表")
    total: int = Field(0, description="总条数")


class FileVersionItem(BaseModel):
    version: int = Field(..., description="版本号")
    oss_key: str = Field(..., description="对象存储 key")
    file_name: str | None = Field(None, description="文件名")
    file_type: str | None = Field(None, description="文件类型")
    file_size: int | None = Field(None, description="文件大小（字节）")
    file_hash: str | None = Field(None, description="内容哈希")
    upload_user_id: str | None = Field(None, description="该版本上传人")
    status: int = Field(1, description="状态")
    created_at: str | None = Field(None, description="创建时间（ISO）")


class FileVersionsData(BaseModel):
    base_key: str = Field(..., description="文档业务键")
    versions: list[FileVersionItem] = Field(default_factory=list, description="版本列表")


class FileBatchItem(BaseModel):
    oss_key: str = Field(..., description="对象存储 key")
    base64: str | None = Field(None, description="文件内容 base64（批量下载以小文件为限）")


# ============ 文档索引（运营后台）============
class DocCreateResult(BaseModel):
    tenant_id: int = Field(..., description="租户 id")
    doc_id: int = Field(..., description="文档索引记录 id（rag_document.id）")
    status: str = Field(..., description="状态（pending/indexing/done/failed）")


class DocStatusData(BaseModel):
    doc_id: int = Field(..., description="文档 id")
    status: str = Field(..., description="索引状态")


# ============ 检索调试（运营后台，SSE chunk 事件的负载形状）============
class RetrieveChunkItem(BaseModel):
    chunk_id: int | None = Field(None, description="切片 id")
    content: str = Field('', description="片段内容")
    doc_name: str | None = Field(None, description="来源文档名")
    page: int | None = Field(None, description="页码")
    score: float | None = Field(None, description="相关度分数")
    parent_id: str | None = Field(None, description="父块 id")


class RetrieveData(BaseModel):
    chunks: list[RetrieveChunkItem] = Field(default_factory=list, description="召回片段")


# ============ 健康检查（运营后台）============
class HealthData(BaseModel):
    status: str = Field(..., description="总体状态")
    redis: bool = Field(..., description="Redis 是否可达")
    mysql: bool = Field(..., description="MySQL 是否可达")
    milvus: bool = Field(..., description="Milvus 集合是否就绪")
    model: bool = Field(..., description="模型是否就绪")
    mcp: bool = Field(..., description="MCP API 服务是否就绪（未启用为 False）")
    nacos: bool = Field(..., description="Nacos 服务发现是否已注册成功（未启用/注册失败为 False）")
    config_center: bool = Field(..., description="配置中心是否已连上（未启用为 False）")


# ============ 配置中心 - 写（审批流，运营后台）============
class ConfigRequestItem(BaseModel):
    id: int = Field(..., description="申请 id")
    namespace: str = Field(..., description="Nacos 命名空间")
    group_id: str = Field(..., description="配置分组")
    data_id: str = Field(..., description="配置 data_id")
    domain: str = Field(..., description="业务域")
    op_type: int = Field(..., description="操作类型：1=新增 2=修改 3=删除")
    title: str | None = Field(None, description="标题")
    content_format: str = Field('json', description="内容格式")
    status: int = Field(..., description="状态")
    applicant: str | None = Field(None, description="申请人")
    reviewer: str | None = Field(None, description="复核人")
    reason: str | None = Field(None, description="变更理由")
    created_at: Any = Field(None, description="创建时间")
    updated_at: Any = Field(None, description="更新时间")


class ConfigRequestListData(BaseModel):
    items: list[ConfigRequestItem] = Field(default_factory=list, description="申请列表")
    total: int = Field(0, description="总条数")


class SyncHistoryResult(BaseModel):
    id: int = Field(..., description="申请 id")
    status: int = Field(..., description="状态")
    nacos_history_nid: int | None = Field(None, description="新内容历史 nid")
    old_history_nid: int | None = Field(None, description="旧内容历史 nid")


# ============ 配置中心 - 读（运营后台）============
class ConfigContentData(BaseModel):
    content: str = Field(..., description="配置内容")


__all__ = [
    "OkData", "IdStatus",
    "QAChunk", "QAResult", "SessionCreated", "SessionItem", "SessionListData", "SessionDetail",
    "FaqItem", "FaqCreateResult", "FaqUpdateResult", "FaqDeleteResult",
    "FileUploadResult", "FileBatchUploadItem",
    "DocumentListItem", "DocumentListData",
    "FileVersionItem", "FileVersionsData",
    "FileBatchItem",
    "DocCreateResult", "DocStatusData",
    "RetrieveChunkItem", "RetrieveData",
    "HealthData",
    "ConfigRequestItem", "ConfigRequestListData", "SyncHistoryResult",
    "ConfigContentData",
]
