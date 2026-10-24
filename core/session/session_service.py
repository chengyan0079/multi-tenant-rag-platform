"""会话服务：编排 Redis 原文 + PostgreSQL 注册表 + 记忆压缩（compaction）。

对外暴露 create / list / get / append_turn / rename / delete 等业务方法。
会话仅按 user_id 隔离（user_id 由网关 OAuth 全局唯一下发）。
"""
import asyncio
import re
from typing import Any

from common.logger import logger
from store.session_store import SessionStore, SessionView
from core.session_memory.memory_service import MemoryService

_TITLE_MAX_LEN = 50
_COMPACT_EVERY_TURNS = 4


_SEP_RE = re.compile(r'[。！？!?.\n]')


def generate_title_from_query(query: str) -> str:
    """首句截断生成标题（零成本）：取第一个句末标点前的文本，截断到 _TITLE_MAX_LEN。"""
    if not query:
        return '新会话'
    m = _SEP_RE.search(query)
    if m:
        query = query[:m.start()].strip()
    else:
        query = query.strip()
    if not query:
        return '新会话'
    return query[:_TITLE_MAX_LEN]


class SessionService:
    def __init__(self, session_store: SessionStore | None = None,
                 memory_service: MemoryService | None = None) -> None:
        self.store = session_store or SessionStore()
        self.memory = memory_service or MemoryService(session_store=self.store)

    async def create(self, user_id: str, title: str | None = None) -> tuple[str, str]:
        """创建会话：Redis 登记 + PostgreSQL 注册表写入；返回 (session_id, title)。"""
        sid = await self.store.create_redis(user_id, '')
        t = title or ''
        await self.store.registry_upsert(user_id, sid, title=t)
        return sid, t

    async def list(self, user_id: str) -> list[SessionView]:
        """当前用户的会话列表（富字段，来自 PostgreSQL 注册表）。"""
        return await self.store.registry_list(user_id)

    async def get(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        """会话详情：注册表元数据 + Redis 原文 + 记忆。"""
        reg = await self.store.registry_get(user_id, session_id)
        if reg is None:
            return None
        history = await self.store.get_history(user_id, session_id)
        memory = await self.memory.get(user_id, session_id)
        return {
            'session_id': session_id,
            'title': reg.title,
            'content': history,
            'memory': memory.to_dict(),
            'created_at': reg.created_at,
            'updated_at': reg.updated_at,
        }

    async def append_turn(self, user_id: str, session_id: str,
                          role: str, content: str,
                          extra: dict[str, Any] | None = None) -> None:
        """追加一条对话，并维护注册表（预览/轮数/首轮标题）与记忆压缩触发。

        extra 为问答明细（turn_id / source / chunks 引用…），只在 assistant 侧传；
        回看时直接从会话原文就能拿到，不必再查数据库。
        """
        await self.store.append_content(user_id, session_id, role, content, extra=extra)
        preview = content[:200]
        if role == 'user':
            reg = await self.store.registry_get(user_id, session_id)
            title = reg.title if reg else ''
            if not title:
                title = generate_title_from_query(content)
                await self.store.set_meta_title(user_id, session_id, title)
            await self.store.registry_upsert(
                user_id, session_id, title=title, preview=preview, inc_turn=True)
            # 记忆压缩：每 _COMPACT_EVERY_TURNS 轮触发一次（异步，不阻塞问答）
            if reg is not None and (reg.turn_count + 1) % _COMPACT_EVERY_TURNS == 0:
                asyncio.create_task(self._safe_compact(user_id, session_id))
        else:
            await self.store.registry_upsert(user_id, session_id, preview=preview)

    async def rename(self, user_id: str, session_id: str, title: str) -> None:
        """用户编辑会话标题。"""
        await self.store.registry_upsert(user_id, session_id, title=title)
        await self.store.set_meta_title(user_id, session_id, title)

    async def delete(self, user_id: str, session_id: str) -> None:
        """删除会话：Redis 原文/元数据 + 记忆 + PostgreSQL 注册表 + 问答明细。"""
        await self.store.delete_redis(user_id, session_id)
        await self.memory.delete(user_id, session_id)
        await self.store.registry_delete(user_id, session_id)
        await self.store.delete_turn_rows(user_id, session_id)

    async def clear_context(self, user_id: str, session_id: str) -> None:
        """删除当前会话的窗口记忆（压缩上下文），不影响对话历史与注册表。"""
        await self.memory.delete(user_id, session_id)

    async def _safe_compact(self, user_id: str, session_id: str) -> None:
        """记忆压缩（容错）：压缩后把摘要回填注册表 memory_summary。"""
        try:
            await self.memory.compact(user_id, session_id)
            mem = await self.memory.get(user_id, session_id)
            if mem and mem.summary:
                await self.store.registry_upsert(
                    user_id, session_id, memory_summary=mem.summary[:200])
        except Exception as e:  # noqa: BLE001
            logger.warning(f'会话记忆压缩失败 [session={session_id}]: {e}')
