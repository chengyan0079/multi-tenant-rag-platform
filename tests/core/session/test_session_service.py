"""SessionService 单测：编排逻辑（标题生成、轮数计数、压缩触发、删除）。

用 FakeStore / FakeMemory 注入，不触达 Redis / MySQL / LLM。
"""
import sys
from unittest import mock

# 本环境缺 greenlet，sqlalchemy.ext.asyncio 无法 import；store.mysql_store 被
# SessionStore 顶层导入，这里用 MagicMock 替换以避免 import 失败（本测试不触达真实注册表）。
sys.modules.setdefault("store.mysql_store", mock.MagicMock())

import asyncio

import pytest

pytest.importorskip("langchain_openai")

from core.session.session_service import SessionService, generate_title_from_query
from store.session_store import SessionView
from store.memory_store import SessionMemory


class FakeMemory:
    def __init__(self) -> None:
        self.mem = SessionMemory()
        self.compacted: list[tuple] = []

    async def get(self, *a, **k) -> SessionMemory:
        return self.mem

    async def delete(self, *a, **k) -> None:
        self.mem = SessionMemory()

    async def compact(self, uid, sid, **k) -> None:
        self.compacted.append((uid, sid))
        self.mem = SessionMemory(summary="s", facts=["f1"])


class FakeStore:
    def __init__(self) -> None:
        self.created: list[tuple] = []
        self.appends: list[tuple] = []
        self.registry: dict[tuple, SessionView] = {}
        self.titles: dict[tuple, str] = {}
        self.deleted: list[tuple] = []
        self.turn_rows_deleted: list[tuple] = []

    async def create_redis(self, uid, sid):
        sid = sid or "auto"
        self.created.append((uid, sid))
        self.registry[(uid, sid)] = SessionView(session_id=sid, title="")
        return sid

    async def append_content(self, uid, sid, role, content, extra=None):
        self.appends.append((uid, sid, role, content))

    async def get_history(self, uid, sid, last=6):
        return [{"role": r[2], "content": r[3]} for r in self.appends if r[1] == sid]

    async def get_meta(self, *a, **k):
        return {}

    async def set_meta_title(self, uid, sid, title):
        self.titles[(uid, sid)] = title
        if (uid, sid) in self.registry:
            self.registry[(uid, sid)].title = title

    async def registry_upsert(self, uid, sid, **kw):
        v = self.registry.setdefault((uid, sid), SessionView(session_id=sid))
        if kw.get("title") is not None:
            v.title = kw["title"]
        if kw.get("preview") is not None:
            v.preview = kw["preview"]
        if kw.get("inc_turn"):
            v.turn_count += 1

    async def registry_get(self, uid, sid):
        return self.registry.get((uid, sid))

    async def registry_list(self, uid):
        return [v for k, v in self.registry.items() if k[0] == uid]

    async def registry_delete(self, uid, sid):
        self.registry.pop((uid, sid), None)

    async def delete_redis(self, uid, sid):
        self.deleted.append((uid, sid))

    async def delete_turn_rows(self, uid, sid):
        self.turn_rows_deleted.append((uid, sid))


def test_create_returns_sid_and_title():
    async def _run():
        svc = SessionService(session_store=FakeStore(), memory_service=FakeMemory())
        sid, title = await svc.create("u1")
        assert sid and title == ""
    asyncio.run(_run())


def test_append_generates_title_counts_turns_and_compacts():
    async def _run():
        store = FakeStore(); mem = FakeMemory()
        svc = SessionService(session_store=store, memory_service=mem)
        sid, _ = await svc.create("u1")
        pairs = [("今天天气怎么样？我想出去玩。", "晴天。"),
                 ("那去公园吧。", "好的。"),
                 ("带点吃的。", "没问题。"),
                 ("几点出发？", "上午九点。")]
        for u, a in pairs:
            await svc.append_turn("u1", sid, "user", u)
            await svc.append_turn("u1", sid, "assistant", a)
        v = store.registry[("u1", sid)]
        assert v.turn_count == 4
        # 第 4 轮用户消息触发一次异步压缩，让出事件循环使其执行完
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(mem.compacted) == 1
        # 首轮自动生成标题（取首句截断）
        assert store.titles[("u1", sid)] == "今天天气怎么样"
    asyncio.run(_run())


def test_delete_calls_store_and_memory():
    async def _run():
        store = FakeStore(); mem = FakeMemory()
        svc = SessionService(session_store=store, memory_service=mem)
        sid, _ = await svc.create("u1")
        await svc.delete("u1", sid)
        assert ("u1", sid) in store.deleted
        assert ("u1", sid) in store.turn_rows_deleted   # 明细随会话一起清掉
        assert mem.mem.summary == ""  # delete 重置记忆
    asyncio.run(_run())


def test_generate_title_from_query():
    assert generate_title_from_query("你好。我是小明。") == "你好"
    assert generate_title_from_query("Hello! How are you?") == "Hello"
    assert generate_title_from_query("") == "新会话"
