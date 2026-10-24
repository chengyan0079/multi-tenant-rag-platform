"""配置中心审批流模型（系统级，无 tenant_id；历史/当前态复用 Nacos）。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, SmallInteger, String, Text
from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column

from common.config import config as app_config
from store.orm_models.base import Base


class ConfigChangeRequest(Base):
    """配置变更申请（审批流，无 tenant_id；历史/当前态复用 Nacos）。"""
    __tablename__ = 'rag_config_change_request'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    # 命名空间统一取 config，不在此处另存一份（Nacos 这里要的是命名空间 ID，不是显示名）
    namespace: Mapped[str] = mapped_column(String(128), nullable=False,
                                           default=app_config.NACOS_CONFIG_NAMESPACE,
                                           comment='Nacos 命名空间（取 config.NACOS_CONFIG_NAMESPACE）')
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, default='DEFAULT_GROUP', comment='Nacos 分组')
    data_id: Mapped[str] = mapped_column(String(256), nullable=False, comment='Nacos 配置ID')
    domain: Mapped[str] = mapped_column(String(32), nullable=False, default='prompt', comment='业务域（如 prompt）')
    op_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment='操作类型（1=新增 2=修改 3=删除 4=回滚）')
    title: Mapped[str | None] = mapped_column(String(256), comment='标题')
    content_new: Mapped[str | None] = mapped_column(Text, comment='拟下发内容（全文，审批前未进 Nacos）')
    old_config_id: Mapped[int | None] = mapped_column(BigInteger, comment='变更前 Nacos config_info.id（CAS 基准引用）')
    old_config_md5: Mapped[str | None] = mapped_column(String(64), comment='申请时 Nacos 当前 md5（CAS 基准）')
    old_history_nid: Mapped[int | None] = mapped_column(BigInteger, comment='执行后回填：变更前内容归档历史 nid')
    content_format: Mapped[str] = mapped_column(String(16), nullable=False, default='json', comment='内容格式（json/text/yaml）')
    src_history_nid: Mapped[int | None] = mapped_column(BigInteger, comment='回滚来源历史 nid')
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, comment='状态（0=待复核 1=通过 2=驳回 3=已执行）')
    applicant: Mapped[str | None] = mapped_column(String(64), comment='申请人')
    reviewer: Mapped[str | None] = mapped_column(String(64), comment='复核人')
    reason: Mapped[str | None] = mapped_column(String(512), comment='变更原因')
    nacos_history_nid: Mapped[int | None] = mapped_column(BigInteger, comment='执行后回填：新内容历史 nid')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_status', 'status'),
        Index('idx_domain', 'domain'),
        Index('idx_data', 'namespace', 'group_id', 'data_id'),
    )


class ConfigReview(Base):
    """配置复核记录（审批流，无 tenant_id）。"""
    __tablename__ = 'rag_config_review'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    request_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='关联申请ID')
    reviewer: Mapped[str | None] = mapped_column(String(64), comment='复核人')
    action: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment='动作（1=通过 2=驳回）')
    comment: Mapped[str | None] = mapped_column(String(512), comment='复核意见')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_request', 'request_id'),
    )
