"""文件模型：文档列表（rag_file）+ 版本历史（rag_file_version）。

一个文档（base_key）在 rag_file 存一行（冗余"当前版本"快照，供列表直查）；
每次上传在 rag_file_version 存一行（版本历史）。文档租户内共享（下载不按用户隔离），
上传人分别记录：rag_file.create_user_id=创建人，rag_file_version.upload_user_id=该版本上传人。

幂等：UNIQUE(tenant_id, base_key) 保证文档唯一；UNIQUE(file_id, file_hash) 保证同文档同内容
只留一个版本（重复上传相同内容不新增版本）。
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String
from sqlalchemy import UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from store.orm_models.base import Base


class File(Base):
    """文档（文件列表）：一个 base_key 一行，冗余当前版本信息。"""
    __tablename__ = 'rag_file'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    base_key: Mapped[str] = mapped_column(String(512), nullable=False, comment='文档业务键（同文档多版本共用）')
    file_name: Mapped[str | None] = mapped_column(String(256), comment='当前版本文件名')
    file_type: Mapped[str | None] = mapped_column(String(16), comment='当前版本文件类型')
    create_user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='创建人（首个上传者 user_id）')
    current_version: Mapped[int] = mapped_column(Integer, default=0, comment='当前版本号')
    current_oss_key: Mapped[str | None] = mapped_column(String(512), comment='当前版本对象存储 key')
    current_file_size: Mapped[int | None] = mapped_column(BigInteger, comment='当前版本文件大小（字节）')
    current_file_hash: Mapped[str | None] = mapped_column(String(64), comment='当前版本内容哈希')
    version_count: Mapped[int] = mapped_column(Integer, default=0, comment='版本总数')
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment='状态（1=有效 0=删除）')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        UniqueConstraint('tenant_id', 'base_key', name='uk_tenant_base'),
        Index('idx_tenant_status', 'tenant_id', 'status', 'updated_at'),
    )


class FileVersion(Base):
    """文件版本历史：一个版本一行。"""
    __tablename__ = 'rag_file_version'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    file_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='所属文档ID（rag_file.id）')
    base_key: Mapped[str] = mapped_column(String(512), nullable=False, comment='文档业务键（冗余便查）')
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment='版本号')
    oss_key: Mapped[str] = mapped_column(String(512), nullable=False, comment='该版本对象存储 key（含版本，不覆盖）')
    source_path: Mapped[str | None] = mapped_column(String(512), comment='原始来源路径')
    file_name: Mapped[str | None] = mapped_column(String(256), comment='文件名')
    file_type: Mapped[str | None] = mapped_column(String(16), comment='文件类型')
    file_size: Mapped[int | None] = mapped_column(BigInteger, comment='文件大小（字节）')
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment='内容哈希（同文档内容去重/幂等）')
    upload_user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='该版本上传人 user_id')
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment='状态（1=有效）')
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, comment='过期时间')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        UniqueConstraint('file_id', 'version', name='uk_file_version'),
        UniqueConstraint('file_id', 'file_hash', name='uk_file_hash'),
        Index('idx_tenant_base', 'tenant_id', 'base_key'),
    )
