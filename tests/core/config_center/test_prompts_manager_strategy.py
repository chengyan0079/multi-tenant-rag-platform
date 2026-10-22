"""
test_prompts_manager_strategy - strategy_select_prompt 单测（pytest + mock）

不依赖真实 Redis/Nacos：mock 掉 prompts_cache.get_prompt。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

import core.config_center.prompts_manager as pm


def test_strategy_select_prompt_nacos_missing_returns_fallback():
    with mock.patch.object(pm, "get_prompt", return_value=None):
        out = pm.RAGPromptsManager.strategy_select_prompt()
    assert "检索策略选择器" in out
    assert "直接检索" in out


def test_strategy_select_prompt_uses_nacos():
    with mock.patch.object(pm, "get_prompt", return_value="自定义策略提示词"):
        out = pm.RAGPromptsManager.strategy_select_prompt()
    assert out == "自定义策略提示词"
