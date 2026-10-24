"""store/orm_models - ORM 模型按功能拆分后的统一出口。

导入本包即把所有表注册到同一 Base.metadata（供 PostgresManager 创建引擎 / Alembic 建表）。
"""
from store.orm_models.base import Base
from store.orm_models.knowledge import Doc, Chunk, IndexTask
from store.orm_models.faq import Faq
from store.orm_models.file import File, FileVersion
from store.orm_models.config import ConfigChangeRequest, ConfigReview
from store.orm_models.session import RagSession, RagTurn
from store.orm_models.api_log import ApiCallLog

__all__ = [
    'Base',
    'Doc', 'Chunk', 'IndexTask',
    'Faq',
    'File', 'FileVersion',
    'ConfigChangeRequest', 'ConfigReview',
    'RagSession', 'RagTurn',
    'ApiCallLog',
]
