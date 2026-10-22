"""聊天停止标记单测：set/is/clear_chat_stop（内存假 Redis）。"""
import asyncio

from app.app_state import app_state
from store import redis_store as rs


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)


def test_chat_stop_roundtrip() -> None:
    app_state.redis = _FakeRedis()
    try:
        asyncio.run(rs.set_chat_stop("u1", "s1"))
        assert asyncio.run(rs.is_chat_stopped("u1", "s1")) is True
        assert asyncio.run(rs.is_chat_stopped("u1", "s2")) is False  # 按 user+session 隔离
        asyncio.run(rs.clear_chat_stop("u1", "s1"))
        assert asyncio.run(rs.is_chat_stopped("u1", "s1")) is False
    finally:
        app_state.redis = None
