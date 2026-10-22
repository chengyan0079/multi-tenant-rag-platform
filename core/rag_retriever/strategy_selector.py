"""
strategy_selector - 检索策略路由（LLM 零样本分类）

在"专业咨询"分支内，决定用哪种检索增强方式：
  DIRECT    直接检索（原问题）
  HYDE      假设性文档嵌入检索（先生成假设答案再检索）
  SUB_QUERY 子查询检索（拆多个子问题分别检索后合并）
  BACKTRACK 回溯问题检索（把复杂长问题抽象为核心问题再检索）

依赖大模型做零样本分类；分类失败/返回非法值统一降级为 DIRECT。
"""
from enum import Enum

from common.config import config
from common.logger import logger
from core.config_center.prompts_manager import RAGPromptsManager
from core.rag_model.llm_client import acomplete


class RetrievalStrategy(str, Enum):
    """检索增强策略（value 与 Nacos 提示词 STRATEGY_SELECT_TEMPLATE 选项保持一致）。"""
    DIRECT = '直接检索'
    HYDE = '假设性文档嵌入检索'
    SUB_QUERY = '子查询检索'
    BACKTRACK = '回溯问题检索'


class StrategySelector:
    """LLM 检索策略路由；关闭或失败时降级 DIRECT。"""

    def __init__(self, enabled: bool = config.STRATEGY_SELECT_ENABLED) -> None:
        self.enabled = enabled

    async def select_strategy(self, query: str) -> RetrievalStrategy:
        if not self.enabled:
            return RetrievalStrategy.DIRECT
        prompt = RAGPromptsManager.strategy_select_prompt()
        raw = await acomplete(f'{prompt}\n\n用户问题：{query}', temperature=0.0, max_tokens=16)
        text = (raw or '').strip().strip('。')
        for s in RetrievalStrategy:
            if text == s.value or text.startswith(s.value):
                return s
        logger.debug(f'检索策略分类无效({text!r})，降级 DIRECT')
        return RetrievalStrategy.DIRECT
