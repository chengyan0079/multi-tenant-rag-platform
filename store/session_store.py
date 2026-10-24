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


def _chunk_field(chunk: Any, name: str, default: Any = None) -> Any:
    """片段既可能是 RetrievedChunk，也可能是 dict（压缩后/测试里），取值兼容两种。"""
    if isinstance(chunk, dict):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _chunk_refs(chunks: list[Any] | None) -> list[dict[str, Any]]:
    """把召回片段压成引用列表：默认只存 doc_name/page/score，开全文才带正文。

    存引用而非全文，是为了不让同一份文本在明细表里再复制一份（文档下线后版本会不一致）。
    """
    if not chunks:
        return []
    fulltext = config.QA_PERSIST_CHUNK_FULLTEXT
    refs: list[dict[str, Any]] = []
    for c in chunks:
        ref = {
            'chunk_id': _chunk_field(c, 'chunk_id'),
            'doc_name': _chunk_field(c, 'doc_name', '') or '',
            'page': _chunk_field(c, 'page', 0) or 0,
            'score': _chunk_field(c, 'rerank_score', 0.0) or 0.0,
        }
        if fulltext:
            ref['text'] = _chunk_field(c, 'content', '') or ''
        refs.append(ref)
    return refs


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

    # ---- 本轮问答明细（turn）结构：集中定义，避免各调用处各拼一份 ----
    def new_turn_id(self) -> str:
        """一轮问答一个 ID（同轮 user/assistant 两条共享），用于 Redis 元素与 MySQL 行对账。"""
        return uuid.uuid4().hex

    def build_turn_payload(
        self,
        *,
        turn_id: str,
        user_id: str,
        session_id: str,
        query: str,
        answer: str,
        tenant_id: int = 0,
        intent: str | None = None,
        source: str = 'rag',
        strategy: str | None = None,
        model: str | None = None,
        chunks: list[Any] | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int | None = None,
        compress_method: str | None = None,
        compress_dropped: int | None = None,
        eval_flag: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """构造一轮问答的持久化数据，返回 (user 元素, assistant 元素, MySQL 行列表)。

        QA 明细（检索/模型/耗时/压缩）只挂在 assistant 侧——它们是产出侧属性；
        user 元素保持轻量，也兼容历史数据里没有这些字段的元素。
        """
        ts = int(time.time())
        refs = _chunk_refs(chunks)
        user_item = {'role': 'user', 'content': query, 'ts': ts, 'turn_id': turn_id}
        asst_item = {
            'role': 'assistant', 'content': answer, 'ts': ts,
            'turn_id': turn_id, 'tenant_id': tenant_id, 'intent': intent, 'source': source,
            'strategy': strategy, 'model': model, 'chunks': refs,
            'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens,
            'latency_ms': latency_ms,
            'compress': {'method': compress_method, 'dropped': compress_dropped},
        }
        rows = [
            {'turn_id': turn_id, 'user_id': user_id, 'session_id': session_id,
             'tenant_id': tenant_id, 'role': 'user', 'content': query,
             'intent': intent, 'eval_flag': eval_flag},
            {'turn_id': turn_id, 'user_id': user_id, 'session_id': session_id,
             'tenant_id': tenant_id, 'role': 'assistant', 'content': answer,
             'intent': intent, 'source': source, 'strategy': strategy, 'model': model,
             'chunks_json': json.dumps(refs, ensure_ascii=False) if refs else None,
             'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens,
             'latency_ms': latency_ms, 'compress_method': compress_method,
             'compress_dropped': compress_dropped, 'eval_flag': eval_flag},
        ]
        return user_item, asst_item, rows

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

    async def append_content(self, user_id: str, session_id: str, role: str, content: str,
                             extra: dict[str, Any] | None = None) -> None:
        """RPUSH 追加一条对话（按用户+会话隔离写入）。

        extra 用于挂问答明细（turn_id / source / chunks 引用…），由调用方按结构传入；
        不传则保持 {role, content, ts}，兼容历史元素。
        """
        if app_state.redis is None:
            return
        try:
            item = {'role': role, 'content': content, 'ts': int(time.time())}
            if extra:
                item.update(extra)
            key = self._conv_key(user_id, session_id)
            await app_state.redis.rpush(key, json.dumps(item))
            await app_state.redis.expire(key, self.content_ttl)
        except Exception as e:
            logger.warning(f'会话追加失败: {e}')

    async def append_turn(self, user_id: str, session_id: str, *items: dict[str, Any]) -> None:
        """把结构化 turn 元素追加到会话 LIST（RPUSH + 续期 + 超额裁剪）。

        一轮两条（user / assistant）一次 RPUSH 进去，保证顺序不会被并发写打乱。
        """
        if app_state.redis is None or not items:
            return
        try:
            key = self._conv_key(user_id, session_id)
            payload = [json.dumps(it, ensure_ascii=False) for it in items]
            await app_state.redis.rpush(key, *payload)
            await app_state.redis.expire(key, self.content_ttl)
            max_turns = config.QA_PERSIST_MAX_TURNS
            if max_turns > 0:
                await app_state.redis.ltrim(key, -max_turns, -1)   # 只保留最近 N 条消息
        except Exception as e:
            logger.warning(f'会话明细追加失败: {e}')

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
    async def persist_turn_rows(self, rows: list[dict[str, Any]]) -> None:
        """问答明细落 MySQL（一轮两条），唯一键冲突视为成功——重试/重放时幂等。"""
        if not rows:
            return
        from sqlalchemy.exc import IntegrityError

        from store.orm_models.session import RagTurn
        try:
            async with get_session() as session:
                session.add_all([RagTurn(**r) for r in rows])
                await session.commit()
        except IntegrityError:
            return   # (turn_id, role) 已写过，忽略
        except Exception as e:
            logger.warning(f'问答明细落库失败: {e}')

    async def delete_turn_rows(self, user_id: str, session_id: str) -> None:
        """删除会话的全部问答明细（与删会话联动）。"""
        from sqlalchemy import delete

        from store.orm_models.session import RagTurn
        try:
            async with get_session() as session:
                await session.execute(delete(RagTurn).where(
                    RagTurn.user_id == user_id, RagTurn.session_id == session_id))
                await session.commit()
        except Exception as e:
            logger.warning(f'问答明细删除失败 [session={session_id}]: {e}')

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
