"""RAG 提示词管理器：取词走"先 Redis 后 Nacos"，按 data_id 取内容构建 PromptTemplate，供生成/检索层调用。本体在 Nacos PROMPT 组。"""
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
