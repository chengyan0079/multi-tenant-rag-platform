"""MemoryStore / MemoryService 单测：存储往返、compaction 解析合并、注入文本。

用内存假 Redis；compaction 的 LLM 调用以 mock 替换，不触达真实网关。
"""
import asyncio
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("langchain_openai")

from app.app_state import app_state
from store.memory_store import MemoryStore
from core.session_memory.memory_service import MemoryService
from store.memory_store import SessionMemory


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self.data.get(key)


@pytest.fixture
def redis():
    r = FakeRedis()
    app_state.redis = r
    yield r
    app_state.redis = None


def test_save_and_get(redis):
    async def _run():
        s = MemoryStore()
        await s.save("u1", "s1", SessionMemory(summary="摘要", facts=["f1", "f2"]))
        m = await s.get("u1", "s1")
        assert m.summary == "摘要"
        assert m.facts == ["f1", "f2"]
    asyncio.run(_run())


def test_compact_parses_json_and_merges(redis):
    async def _run():
        store = MemoryStore()
        svc = MemoryService(memory_store=store)
        await store.save("u1", "s1", SessionMemory(summary="旧摘要", facts=["旧事实"]))
        with mock.patch("core.session_memory.memory_service.acomplete",
                        new=mock.AsyncMock(return_value='{"summary":"新摘要","facts":["旧事实","新事实"]}')):
            result = await svc.compact("u1", "s1",
                                       recent_history=[{"role": "user", "content": "x"}])
        assert result.summary == "新摘要"
        assert "新事实" in result.facts and "旧事实" in result.facts
        persisted = await store.get("u1", "s1")
        assert persisted.summary == "新摘要"
    asyncio.run(_run())


def test_compact_failure_keeps_old(redis):
    async def _run():
        store = MemoryStore()
        svc = MemoryService(memory_store=store)
        await store.save("u1", "s1", SessionMemory(summary="旧", facts=["a"]))
        with mock.patch("core.session_memory.memory_service.acomplete",
                        new=mock.AsyncMock(return_value="不是合法json")):
            result = await svc.compact("u1", "s1",
                                       recent_history=[{"role": "user", "content": "x"}])
        assert result.summary == "旧"
        assert "a" in result.facts
    asyncio.run(_run())


def test_build_injection_text():
    assert MemoryService.build_injection_text(SessionMemory()) == ""
    txt = MemoryService.build_injection_text(SessionMemory(summary="s", facts=["a", "b"]))
    assert "摘要: s" in txt and "- a" in txt and "- b" in txt
