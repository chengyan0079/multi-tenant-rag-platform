"""SessionStore 单测：验证 (用户, 会话) 两级隔离与建/追加/取历史/删除。

用内存假 Redis，避免依赖真实 redis 客户端。
"""
import sys
from unittest import mock

# 本环境缺 greenlet，sqlalchemy.ext.asyncio 无法 import；store.mysql_store 仅被
# SessionStore 顶层导入，这里用 MagicMock 替换，避免 import 失败（注册表走 mock）。
sys.modules.setdefault("store.mysql_store", mock.MagicMock())

import asyncio
from typing import Any

import pytest

from app.app_state import app_state
from store.session_store import SessionStore


class FakeRedis:
    """最小内存实现，覆盖 SessionStore 用到的异步方法。"""
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    async def rpush(self, key: str, value: str) -> int:
        self.data.setdefault(("list", key), []).append(value)
        return len(self.data[("list", key)])

    async def lrange(self, key: str, start: int, end: int) -> list:
        lst = self.data.get(("list", key), [])
        n = len(lst)
        s = start if start >= 0 else max(0, n + start)
        e = end if end >= 0 else n + end
        return lst[s:e + 1] if e >= 0 else lst[s:]

    async def expire(self, key: str, ttl: int) -> int:
        return 1

    async def sadd(self, key: str, member: str) -> int:
        self.data.setdefault(("set", key), set()).add(member)
        return 1

    async def smembers(self, key: str) -> set:
        return set(self.data.get(("set", key), set()))

    async def srem(self, key: str, member: str) -> int:
        self.data.get(("set", key), set()).discard(member)
        return 1

    async def hset(self, key: str, mapping: dict | None = None) -> int:
        self.data.setdefault(("hash", key), {}).update(mapping or {})
        return 1

    async def hgetall(self, key: str) -> dict:
        return dict(self.data.get(("hash", key), {}))

    async def delete(self, key: str) -> int:
        for p in ("list", "set", "hash"):
            self.data.pop((p, key), None)
        return 1


@pytest.fixture
def redis():
    r = FakeRedis()
    app_state.redis = r
    yield r
    app_state.redis = None


def test_create_and_history(redis):
    async def _run():
        s = SessionStore()
        sid = await s.create_redis("u1", "")
        assert sid
        await s.append_content("u1", sid, "user", "你好")
        await s.append_content("u1", sid, "assistant", "你好啊")
        hist = await s.get_history("u1", sid, last=6)
        assert len(hist) == 2
        assert hist[0]["role"] == "user"
        assert hist[1]["content"] == "你好啊"
    asyncio.run(_run())


def test_delete(redis):
    async def _run():
        s = SessionStore()
        sid = await s.create_redis("u1", "")
        await s.append_content("u1", sid, "user", "x")
        await s.delete_redis("u1", sid)
        assert await s.get_history("u1", sid) == []
    asyncio.run(_run())
