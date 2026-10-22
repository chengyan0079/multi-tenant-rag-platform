"""
prompts_manager - RAG 提示词管理器（正式模块）

设计：
1. 把提示词工程与业务编排逻辑剥离（集中调优 / A/B 测试只改这里或 Nacos 里的配置）。
2. 取提示词走 "先 Redis 再 Nacos"：运行时优先命中 Redis 缓存，未命中回源 Nacos 配置中心
   （PROMPT 组，namespace=rag-ultra），回源成功回填 Redis。详见 core.config_center.prompts_cache。

提示词本体存于 Nacos 配置中心 PROMPT 组，data_id 即下方常量名：
  CORE_RAG_TEMPLATE / HYDE_GENERATION_TEMPLATE / SUBQ_GENERATION_TEMPLATE / BACK_GENERATION_TEMPLATE
  / STRATEGY_SELECT_TEMPLATE
本模块只负责按 data_id 取内容并构建 LangChain PromptTemplate，供生成层 / 检索层等业务调用。
"""
from typing import Any

from common.config import config
from core.config_center.groups import ConfigGroup
from core.config_center.prompts_cache import get_prompt

# Nacos 配置中心里 PROMPT 组的 data_id（与 Nacos 中配置一一对应）
CORE_RAG_TEMPLATE_ID = 'CORE_RAG_TEMPLATE'
HYDE_GENERATION_TEMPLATE_ID = 'HYDE_GENERATION_TEMPLATE'
SUBQ_GENERATION_TEMPLATE_ID = 'SUBQ_GENERATION_TEMPLATE'
BACK_GENERATION_TEMPLATE_ID = 'BACK_GENERATION_TEMPLATE'
STRATEGY_SELECT_TEMPLATE_ID = 'STRATEGY_SELECT_TEMPLATE'


class RAGPromptsManager:
    """RAG 系统中提示词模板管理器：按 data_id 从缓存/Nacos 取内容并构建 PromptTemplate。"""

    @staticmethod
    def _load(data_id: str) -> str:
        """从缓存或 Nacos 取提示词原文；缺失抛错，交由调用方决定降级。"""
        content = get_prompt(data_id, ConfigGroup.PROMPT.value)
        if content is None:
            raise RuntimeError(f'Nacos 配置中心缺少提示词配置: data_id={data_id}')
        return content

    @staticmethod
    def core_rag_prompt(tel: str = config.CONTACT_TEL) -> Any:
        """直接回答提示词模板（含 {context}{question}{tel} 占位符）。"""
        from langchain_core.prompts import PromptTemplate
        return PromptTemplate.from_template(
            RAGPromptsManager._load(CORE_RAG_TEMPLATE_ID)
        ).partial(tel=tel)

    @staticmethod
    def hyde_gen_prompt() -> Any:
        """假设性文档嵌入生成提示词模板（{query}）。"""
        from langchain_core.prompts import PromptTemplate
        return PromptTemplate.from_template(
            RAGPromptsManager._load(HYDE_GENERATION_TEMPLATE_ID)
        )

    @staticmethod
    def subq_gen_prompt() -> Any:
        """子查询拆分提示词模板（{query}）。"""
        from langchain_core.prompts import PromptTemplate
        return PromptTemplate.from_template(
            RAGPromptsManager._load(SUBQ_GENERATION_TEMPLATE_ID)
        )

    @staticmethod
    def back_gen_prompt() -> Any:
        """回溯问题生成提示词模板（{query}）。"""
        from langchain_core.prompts import PromptTemplate
        return PromptTemplate.from_template(
            RAGPromptsManager._load(BACK_GENERATION_TEMPLATE_ID)
        )

    @staticmethod
    def strategy_select_prompt() -> str:
        """检索策略路由提示词（零样本分类指令）；Nacos 缺配置时返回内置兜底。"""
        try:
            return RAGPromptsManager._load(STRATEGY_SELECT_TEMPLATE_ID)
        except Exception:  # noqa: BLE001
            return (
                "你是一个检索策略选择器。根据用户问题判断最适合的检索增强策略，只返回策略名本身（不要解释）。\n"
                "可选策略：\n- 直接检索：问题明确、可直接用原问题检索\n"
                "- 假设性文档嵌入检索：问题需要背景知识，先生成假设答案再检索\n"
                "- 子查询检索：问题复杂可拆成多个子问题分别检索\n"
                "- 回溯问题检索：问题冗长复杂，先抽象出核心问题再检索\n"
                "只返回以下之一：直接检索 / 假设性文档嵌入检索 / 子查询检索 / 回溯问题检索"
            )
