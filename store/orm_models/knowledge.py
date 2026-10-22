"""知识域模型：document / chunk / index_task（租户级，无 kb_id）。

文档/切片直接归属租户（tenant_id），不再挂知识库；检索仅按租户 + scope 隔离，
与 MinIO 存储（t{tenant_id}/ 前缀）、FAQ（租户级）保持一致。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, Integer, JSON, SmallInteger, String, Text,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from store.orm_models.base import Base


class Doc(Base):
    """文档解析/索引记录（点击“解析”时创建；文件元数据归 rag_file_upload，此处经 file_upload_id 关联）。"""
    __tablename__ = 'rag_document'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    file_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='来源上传记录ID（rag_file_upload.id）')
    doc_type: Mapped[int] = mapped_column(SmallInteger, default=0, comment='文档类型（0=普通文档 1=FAQ来源；对应切片 scope）')
    file_hash: Mapped[str | None] = mapped_column(String(64), comment='解析时文件内容哈希快照（幂等/去重）')
    status: Mapped[int] = mapped_column(SmallInteger, comment='解析状态（0=待解析 1=解析中 2=已完成 3=失败）')
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, comment='生成切片数')
    err_msg: Mapped[str | None] = mapped_column(String(512), comment='解析错误信息')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_tenant_status', 'tenant_id', 'status'),
        Index('idx_file_upload', 'file_upload_id'),
    )


class Chunk(Base):
    """切片（文档/FAQ 切分后的最小检索单元）。"""
    __tablename__ = 'rag_chunk'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='所属解析记录ID（rag_document.id）')
    scope: Mapped[int] = mapped_column(SmallInteger, default=0, comment='范围（0=doc 1=faq）')
    ref_id: Mapped[int] = mapped_column(BigInteger, default=0, comment='关联ID（scope=1 时指向 rag_faq.id）')
    seq: Mapped[int] = mapped_column(Integer, comment='序号')
    content: Mapped[str] = mapped_column(Text, comment='切片内容')
    token_count: Mapped[int] = mapped_column(Integer, comment='token 数')
    meta: Mapped[dict] = mapped_column(JSON, comment='元数据（JSON）')
    enabled: Mapped[int] = mapped_column(SmallInteger, default=1, comment='是否启用（1=启用 0=停用）')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_doc', 'tenant_id', 'doc_id'),
    )


class IndexTask(Base):
    """索引任务（文档解析/嵌入入库的异步任务）。"""
    __tablename__ = 'rag_index_task'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    doc_id: Mapped[int] = mapped_column(BigInteger, comment='关联解析记录ID（rag_document.id）')
    stage: Mapped[str] = mapped_column(String(16), comment='阶段（如 parse/embed）')
    status: Mapped[int] = mapped_column(SmallInteger, comment='状态')
    retry_count: Mapped[int] = mapped_column(Integer, default=0, comment='重试次数')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_status', 'status', 'stage'),
    )
