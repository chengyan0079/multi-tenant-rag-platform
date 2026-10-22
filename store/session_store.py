"""会话存储（store 数据访问层）：Redis 对话原文/元数据 + MySQL 注册表（rag_session）。

- 对话原文：Redis LIST，TTL=30d
- 注册表：MySQL rag_session（标题/预览/轮数/时间），保证列表可长期回看
隔离维度 (user_id, session_id)，跨用户不可见（user_id 由网关全局唯一下发）。
SessionView 为注册表轻量读投影（存储型 DTO），与 store 同层定义，避免 store→core 反向依赖。
"""
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from common.config import config
from common.logger import logger
from app.app_state import app_state
from store.redis_keys import KeyPrefix, build_key
from store.mysql_store import get_session


@dataclass
class SessionView:
    """会话列表/详情的轻量视图（注册表读投影）。"""
    session_id: str
    title: str = ''
    preview: str = ''
    memory_summary: str = ''
    turn_count: int = 0
    created_at: Any = None
    updated_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'session_id': self.session_id,
            'title': self.title,
            'preview': self.preview,
            'memory_summary': self.memory_summary,
            'turn_count': self.turn_count,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class SessionStore:
    def __init__(self, content_ttl: int = config.REDIS_TTL_SESSION_CONTENT) -> None:
        self.content_ttl = content_ttl

    # ---- key 构造（集中在 store.redis_keys） ----
    def _conv_key(self, user_id: str, session_id: str) -> str:
        return build_key(KeyPrefix.USER, str(user_id), KeyPrefix.SESSION, session_id)

    def _index_key(self, user_id: str) -> str:
        return build_key(KeyPrefix.USER, str(user_id), 'sessions')

    def _meta_key(self, user_id: str, session_id: str) -> str:
        return build_key(KeyPrefix.USER, str(user_id), KeyPrefix.SESSION, session_id, 'meta')

    # ---- Redis：对话原文 / 元数据 / 索引 ----
    async def create_redis(self, user_id: str, session_id: str) -> str:
        """登记会话到 Redis 索引并写创建时间元数据。"""
        sid = session_id or uuid.uuid4().hex
        if app_state.redis is None:
            return sid
        try:
            t = self.content_ttl
            idx = self._index_key(user_id)
            await app_state.redis.sadd(idx, sid)
            await app_state.redis.expire(idx, t)
            mk = self._meta_key(user_id, sid)
            await app_state.redis.hset(mk, mapping={'created_at': str(int(time.time()))})
            await app_state.redis.expire(mk, t)
        except Exception as e:
            logger.warning(f'创建会话 Redis 失败 [user={user_id}]: {e}')
        return sid

    async def append_content(self, user_id: str, session_id: str, role: str, content: str) -> None:
        """原子 RPUSH 追加一轮对话（按用户+会话隔离写入）。"""
        if app_state.redis is None:
            return
        try:
            item = {'role': role, 'content': content, 'ts': int(time.time())}
            key = self._conv_key(user_id, session_id)
            await app_state.redis.rpush(key, json.dumps(item))
            await app_state.redis.expire(key, self.content_ttl)
        except Exception as e:
            logger.warning(f'会话追加失败: {e}')

    async def get_history(self, user_id: str, session_id: str, last: int = 6) -> list[dict[str, Any]]:
        """取最近 last 轮历史（按用户隔离读取）。"""
        if app_state.redis is None:
            return []
        try:
            key = self._conv_key(user_id, session_id)
            raw = await app_state.redis.lrange(key, -last, -1)
            return [json.loads(r) for r in raw]
        except Exception:
            return []

    async def get_meta(self, user_id: str, session_id: str) -> dict[str, Any]:
        if app_state.redis is None:
            return {}
        try:
            meta = await app_state.redis.hgetall(self._meta_key(user_id, session_id))
            if not meta:
                return {}
            return {k: (v.decode() if isinstance(v, bytes) else v) for k, v in meta.items()}
        except Exception:
            return {}

    async def set_meta_title(self, user_id: str, session_id: str, title: str) -> None:
        if app_state.redis is None:
            return
        try:
            mk = self._meta_key(user_id, session_id)
            await app_state.redis.hset(mk, 'title', title)
            await app_state.redis.expire(mk, self.content_ttl)
        except Exception as e:
            logger.warning(f'设置会话标题失败: {e}')

    async def delete_redis(self, user_id: str, session_id: str) -> None:
        if app_state.redis is None:
            return
        try:
            await app_state.redis.delete(self._conv_key(user_id, session_id))
            await app_state.redis.delete(self._meta_key(user_id, session_id))
            await app_state.redis.srem(self._index_key(user_id), session_id)
        except Exception as e:
            logger.warning(f'删除会话 Redis 失败: {e}')

    # ---- MySQL：会话注册表（rag_session） ----
    async def registry_upsert(self, user_id: str, session_id: str, *,
                              title: str | None = None, preview: str | None = None,
                              memory_summary: str | None = None, turn_count: int | None = None,
                              inc_turn: bool = False) -> None:
        """写入/更新会话注册表行（按需更新给定字段；inc_turn 自增轮数）。"""
        from sqlalchemy import select
        from store.orm_models.session import RagSession
        try:
            async with get_session() as session:
                obj = (await session.execute(
                    select(RagSession).where(
                        RagSession.user_id == user_id,
                        RagSession.session_id == session_id,
                    )
                )).scalar_one_or_none()
                if obj is None:
                    obj = RagSession(
                        user_id=user_id, session_id=session_id,
                        title=title or '', preview=preview or '',
                        memory_summary=memory_summary or '',
                        turn_count=turn_count or 0,
                    )
                    session.add(obj)
                else:
                    if title is not None:
                        obj.title = title
                    if preview is not None:
                        obj.preview = preview
                    if memory_summary is not None:
                        obj.memory_summary = memory_summary
                    if turn_count is not None:
                        obj.turn_count = turn_count
                    if inc_turn:
                        obj.turn_count = (obj.turn_count or 0) + 1
                await session.commit()
        except Exception as e:
            logger.warning(f'会话注册表写入失败 [session={session_id}]: {e}')

    async def registry_list(self, user_id: str) -> list[SessionView]:
        """按 用户 列出全部会话（富字段视图），按最近更新排序。"""
        from sqlalchemy import select
        from store.orm_models.session import RagSession
        try:
            async with get_session() as session:
                rows = (await session.execute(
                    select(RagSession).where(
                        RagSession.user_id == user_id
                    ).order_by(RagSession.updated_at.desc())
                )).scalars().all()
                return [SessionView(
                    session_id=r.session_id, title=r.title, preview=r.preview,
                    memory_summary=r.memory_summary, turn_count=r.turn_count,
                    created_at=r.created_at, updated_at=r.updated_at,
                ) for r in rows]
        except Exception as e:
            logger.warning(f'会话列表读取失败 [user={user_id}]: {e}')
            return []

    async def registry_get(self, user_id: str, session_id: str) -> SessionView | None:
        from sqlalchemy import select
        from store.orm_models.session import RagSession
        try:
            async with get_session() as session:
                r = (await session.execute(
                    select(RagSession).where(
                        RagSession.user_id == user_id,
                        RagSession.session_id == session_id,
                    )
                )).scalar_one_or_none()
                if r is None:
                    return None
                return SessionView(
                    session_id=r.session_id, title=r.title, preview=r.preview,
                    memory_summary=r.memory_summary, turn_count=r.turn_count,
                    created_at=r.created_at, updated_at=r.updated_at,
                )
        except Exception as e:
            logger.warning(f'会话读取失败 [session={session_id}]: {e}')
            return None

    async def registry_delete(self, user_id: str, session_id: str) -> None:
        from sqlalchemy import select
        from store.orm_models.session import RagSession
        try:
            async with get_session() as session:
                obj = (await session.execute(
                    select(RagSession).where(
                        RagSession.user_id == user_id,
                        RagSession.session_id == session_id,
                    )
                )).scalar_one_or_none()
                if obj is not None:
                    await session.delete(obj)
                    await session.commit()
        except Exception as e:
            logger.warning(f'会话注册表删除失败 [session={session_id}]: {e}')
