"""会话注册表 ORM 模型（MySQL 持久化，对应 schema.sql 的 rag_session 表）。

只按 user_id 隔离（user_id 由网关 OAuth 全局唯一下发，跨租户不重复），不挂 tenant_id；
对话原文仍存 Redis（热路径）。见 docs/访问模型与会话架构决策.md §2.2。
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from store.orm_models.base import Base


class RagSession(Base):
    """会话注册表：保证会话列表可长期回看（不受 Redis TTL 影响）。"""
    __tablename__ = 'rag_session'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment='主键')
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='用户ID')
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='会话ID')
    title: Mapped[str] = mapped_column(String(255), nullable=False, default='', comment='会话标题')
    preview: Mapped[str] = mapped_column(String(512), nullable=False, default='', comment='最近一条预览')
    memory_summary: Mapped[str] = mapped_column(String(512), nullable=False, default='', comment='记忆摘要')
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment='对话轮数')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        UniqueConstraint('user_id', 'session_id', name='uk_user_session'),
        Index('idx_user', 'user_id'),
    )
