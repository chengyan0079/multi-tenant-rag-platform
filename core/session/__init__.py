"""会话模块：独立管理会话实体（Redis 原文 + PostgreSQL 注册表 + 记忆压缩触发）。

设计见 docs/访问模型与会话架构决策.md §2。隔离维度 (user_id, session_id)；user_id 由网关全局唯一下发。
"""
from store.session_store import SessionView, SessionStore
from core.session.session_service import SessionService, generate_title_from_query

__all__ = ['SessionView', 'SessionStore', 'SessionService', 'generate_title_from_query']
