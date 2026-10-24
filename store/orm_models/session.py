"""会话注册表 ORM 模型（PostgreSQL 持久化，对应 schema_postgresql.sql 的 rag_session 表）。

只按 user_id 隔离（user_id 由网关 OAuth 全局唯一下发，跨租户不重复），不挂 tenant_id；
对话原文仍存 Redis（热路径）。见 docs/访问模型与会话架构决策.md §2.2。
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
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


class RagTurn(Base):
    """问答明细：一轮两条（user / assistant），与 Redis 会话原文元素 1:1。

    落库只为回看、审计、统计与评测取数；不做任何质量评估计算（那是 RAGAS 的事）。
    隔离按 user_id，tenant_id 仅作统计列，不参与查询过滤。
    """
    __tablename__ = 'rag_turn'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment='主键')
    turn_id: Mapped[str] = mapped_column(String(32), nullable=False, comment='一轮问答ID（同轮两条共享）')
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='用户ID')
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, comment='会话ID')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment='租户ID（仅统计）')
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment='消息角色：user|assistant')
    content: Mapped[str] = mapped_column(Text, nullable=False, comment='消息正文')
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True, comment='意图：通用知识|专业咨询')
    source: Mapped[str | None] = mapped_column(String(16), nullable=True, comment='产出来源：cache|faq|rag|empty')
    strategy: Mapped[str | None] = mapped_column(String(32), nullable=True, comment='检索策略')
    model: Mapped[str | None] = mapped_column(String(64), nullable=True, comment='生成模型')
    chunks_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment='召回片段引用 JSON')
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment='提示词 token')
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment='生成 token')
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment='端到端耗时')
    compress_method: Mapped[str | None] = mapped_column(String(16), nullable=True, comment='上下文压缩方式')
    compress_dropped: Mapped[int | None] = mapped_column(Integer, nullable=True, comment='压缩丢弃片段数')
    eval_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment='评测采样标记')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')

    __table_args__ = (
        UniqueConstraint('turn_id', 'role', name='uk_turn_role'),
        Index('idx_user_session', 'user_id', 'session_id'),
        Index('idx_tenant_created', 'tenant_id', 'created_at'),
        Index('idx_created', 'created_at'),
    )
