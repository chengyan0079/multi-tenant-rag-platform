"""FAQ 高频问题模型（租户级，无 kb_id；启动时全量载入 Redis，字面精确匹配）。question_type 与意图分类标签同源（0=通用知识 1=专业咨询），作训练数据源，枚举扩展无需迁移。"""
from datetime import datetime
from enum import IntEnum

from sqlalchemy import BigInteger, DateTime, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from store.orm_models.base import Base


class QuestionType(IntEnum):
    """问题类型 —— 取值严格对齐意图分类器标签（classifier.py: idx_to_label）。

    0=通用知识 / 1=专业咨询。新增意图只需在此追加成员，DB 列 TINYINT 无需改表。
    """
    GENERAL = 0       # 通用知识
    PROFESSIONAL = 1  # 专业咨询


class Faq(Base):
    """FAQ（标准问答对，命中优先于 RAG 兜底；租户级高频问题）。"""
    __tablename__ = 'rag_faq'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment='主键')
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='租户ID')
    question: Mapped[str] = mapped_column(String(512), nullable=False, comment='标准问题')
    answer: Mapped[str] = mapped_column(Text, nullable=False, comment='标准答案')
    similar_questions: Mapped[str] = mapped_column(Text, comment='相似问题（多行文本，命中候选）')
    question_type: Mapped[int] = mapped_column(
        SmallInteger, default=QuestionType.GENERAL, nullable=False,
        comment='问题类型（0=通用知识 1=专业咨询，与意图分类器标签对齐）',
    )
    enabled: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False, comment='是否启用（1=启用 0=停用）')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    __table_args__ = (
        Index('idx_tenant_enabled', 'tenant_id', 'enabled'),
        Index('idx_tenant_type', 'tenant_id', 'question_type'),
    )
