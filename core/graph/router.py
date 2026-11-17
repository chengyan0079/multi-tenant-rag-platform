"""图路路由：FAQ/通用知识前置 + 关键词判问题类型 + 查询参数兜底。

只做"是否走图、走哪条查询"的判断；查询执行与融合在 fusion.py。
"""
from __future__ import annotations

import re
from typing import Any

from common.config import config

RULES: list[tuple[str, str]] = [
    (r'为什么|原因|根因|导致|怎么引起|改了.*为什么', 'rootcause'),
    (r'影响|波及|哪些|谁受影响|影响面', 'impact'),
    (r'怎么处理|怎么恢复|处置|runbook|上次.*怎么|历史.*事故', 'runbook'),
]

_FAULT_ID_RE = re.compile(r'INC-[0-9A-Za-z-]{4,}')


def pick_qtype(query: str) -> str | None:
    """按关键词判问题类型；未命中返回 None（走纯向量）。"""
    for pattern, qtype in RULES:
        if re.search(pattern, query):
            return qtype
    return None


def should_use_graph(query: str, intent: str, faq_hit: bool) -> str | None:
    """图路前置判断：总开关 / FAQ 未命中 / 专业咨询 / 关键词命中，任一不满足返回 None。"""
    if not config.GRAPH_ENABLED:
        return None
    if faq_hit or intent == '通用知识':
        return None
    return pick_qtype(query)


def build_query_params(qtype: str, query: str) -> dict[str, Any]:
    """为 Cypher 准备参数：库名默认当前库；故障号从 query 正则提取，缺失则不限定。"""
    params: dict[str, Any] = {}
    if qtype == 'impact':
        params['db_name'] = config.POSTGRES_DATABASE
    elif qtype == 'runbook':
        m = _FAULT_ID_RE.search(query)
        if m:
            params['fault_id'] = m.group(0)
    return params
