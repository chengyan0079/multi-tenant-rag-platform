"""
test_strategy_selector - 检索策略路由单测（pytest + mock）

mock 掉 prompts_manager 的策略提示词与 acomplete（LLM 调用）。
用 asyncio.run 包裹异步逻辑，避免依赖 pytest-asyncio 插件。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

import core.rag_retriever.strategy_selector as m
from core.rag_retriever.strategy_selector import RetrievalStrategy


def test_select_sub_query():
    async def _run():
        with mock.patch.object(m.RAGPromptsManager, "strategy_select_prompt", return_value="p"), \
             mock.patch.object(m, "acomplete", return_value="子查询检索"):
            sel = m.StrategySelector(enabled=True)
            assert await sel.select_strategy("如何办理") == RetrievalStrategy.SUB_QUERY
    asyncio.run(_run())


def test_select_hyde_by_prefix():
    async def _run():
        with mock.patch.object(m.RAGPromptsManager, "strategy_select_prompt", return_value="p"), \
             mock.patch.object(m, "acomplete", return_value="假设性文档嵌入检索（推荐）"):
            sel = m.StrategySelector(enabled=True)
            assert await sel.select_strategy("问题") == RetrievalStrategy.HYDE
    asyncio.run(_run())


def test_select_invalid_falls_back_direct():
    async def _run():
        with mock.patch.object(m.RAGPromptsManager, "strategy_select_prompt", return_value="p"), \
             mock.patch.object(m, "acomplete", return_value="乱七八糟"):
            sel = m.StrategySelector(enabled=True)
            assert await sel.select_strategy("q") == RetrievalStrategy.DIRECT
    asyncio.run(_run())


def test_disabled_returns_direct_without_llm():
    async def _run():
        sel = m.StrategySelector(enabled=False)
        with mock.patch.object(m, "acomplete") as ac:
            assert await sel.select_strategy("q") == RetrievalStrategy.DIRECT
            ac.assert_not_called()
    asyncio.run(_run())
