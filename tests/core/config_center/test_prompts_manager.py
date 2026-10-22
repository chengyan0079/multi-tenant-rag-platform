"""
test_prompts_manager - RAGPromptsManager 单元测试（pytest + mock）

mock prompts_cache.get_prompt，验证按 data_id 取内容、变量解析、tel 固定、缺配置抛错。
依赖 langchain_core 构建 PromptTemplate（无则整模块跳过，不影响主链路测试）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

import pytest

import core.config_center.prompts_manager as pm

pytest.importorskip("langchain_core")


def test_core_rag_prompt_variables_and_tel():
    with mock.patch.object(pm, "get_prompt", return_value="上下文{context} 问题{question} 电话{tel}"):
        tpl = pm.RAGPromptsManager.core_rag_prompt(tel="123")
    # tel 已被 partial 固定，不再作为模板变量
    assert set(tpl.input_variables) == {"context", "question"}
    assert tpl.template == "上下文{context} 问题{question} 电话{tel}"


def test_hyde_subq_back_use_query_var():
    for method in (
        pm.RAGPromptsManager.hyde_gen_prompt,
        pm.RAGPromptsManager.subq_gen_prompt,
        pm.RAGPromptsManager.back_gen_prompt,
    ):
        with mock.patch.object(pm, "get_prompt", return_value="查询{query}"):
            tpl = method()
        assert tpl.input_variables == ["query"]


def test_missing_config_raises():
    with mock.patch.object(pm, "get_prompt", return_value=None):
        with pytest.raises(RuntimeError):
            pm.RAGPromptsManager.core_rag_prompt()
