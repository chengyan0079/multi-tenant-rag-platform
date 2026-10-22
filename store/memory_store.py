"""会话记忆存储（store 数据访问层）：Redis 存 {summary, facts, updated_at}。

隔离维度 (user_id, session_id)，与对话原文同前缀、独立键后缀 `:memory`。
TTL 取自配置 common.config.REDIS_TTL_MEMORY。
SessionMemory 为记忆值对象（存储型），与 store 同层定义，避免 store→core 反向依赖。
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any

from common.config import config
from common.logger import logger
from app.app_state import app_state
from store.redis_keys import KeyPrefix, build_key


@dataclass
class SessionMemory:
    """会话级长期记忆：摘要 + 结构化事实。"""
    summary: str = ''
    facts: list[str] = field(default_factory=list)
    updated_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {'summary': self.summary, 'facts': self.facts, 'updated_at': self.updated_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'SessionMemory':
        return cls(
            summary=d.get('summary') or '',
            facts=list(d.get('facts') or []),
            updated_at=d.get('updated_at'),
        )


class MemoryStore:
    def __init__(self, ttl: int = config.REDIS_TTL_MEMORY) -> None:
        self.ttl = ttl

    def _mem_key(self, user_id: str, session_id: str) -> str:
        return build_key(KeyPrefix.USER, str(user_id), KeyPrefix.SESSION, session_id, 'memory')

    async def get(self, user_id: str, session_id: str) -> SessionMemory:
        if app_state.redis is None:
            return SessionMemory()
        try:
            raw = await app_state.redis.get(self._mem_key(user_id, session_id))
            if not raw:
                return SessionMemory()
            return SessionMemory.from_dict(json.loads(raw))
        except Exception:
            return SessionMemory()

    async def save(self, user_id: str, session_id: str, memory: SessionMemory) -> None:
        if app_state.redis is None:
            return
        try:
            payload = {'summary': memory.summary, 'facts': memory.facts,
                       'updated_at': int(time.time())}
            await app_state.redis.set(
                self._mem_key(user_id, session_id), json.dumps(payload), ex=self.ttl)
        except Exception as e:
            logger.warning(f'记忆写入失败 [session={session_id}]: {e}')

    async def delete(self, user_id: str, session_id: str) -> None:
        if app_state.redis is None:
            return
        try:
            await app_state.redis.delete(self._mem_key(user_id, session_id))
        except Exception as e:
            logger.warning(f'记忆删除失败 [session={session_id}]: {e}')
