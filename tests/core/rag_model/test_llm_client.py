"""
test_llm_client - 共享 LLM 助手单测（pytest + mock）

不依赖真实 LLM 网关：mock ChatOpenAI 与 ainvoke。
用 asyncio.run 包裹异步逻辑，避免依赖 pytest-asyncio 插件。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import pytest

pytest.importorskip("langchain_openai")

from unittest import mock

import core.rag_model.llm_client as m


def test_build_llm_uses_config():
    fake = mock.MagicMock()
    with mock.patch.object(m, "ChatOpenAI", fake):
        llm = m.build_llm(temperature=0.1, max_tokens=123, streaming=True)
    assert llm is fake.return_value
    _, kwargs = fake.call_args
    assert kwargs["temperature"] == 0.1
    assert kwargs["max_tokens"] == 123
    assert kwargs["streaming"] is True


def test_acomplete_success():
    async def _run():
        fake_ainvoke = mock.AsyncMock(return_value=mock.MagicMock(content="hi"))
        fake_llm = mock.MagicMock(ainvoke=fake_ainvoke)
        with mock.patch.object(m, "build_llm", return_value=fake_llm):
            out = await m.acomplete("prompt")
        assert out == "hi"
        fake_ainvoke.assert_awaited_once_with("prompt")
    asyncio.run(_run())


def test_acomplete_failure_returns_empty():
    async def _run():
        with mock.patch.object(m, "build_llm", side_effect=RuntimeError("boom")):
            out = await m.acomplete("prompt")
        assert out == ""
    asyncio.run(_run())
