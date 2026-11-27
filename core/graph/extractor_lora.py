"""LoRA 故障要素抽取：vLLM OpenAI 兼容接口 + guided_json + 多次采样置信度。

模型只产出扁平要素，不产出关系名；节点/边由调用方按 schema 字段映射建。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from common.config import config, BASE_DIR
from common.logger import logger

# ChatOpenAI 惰性导入：置信度/闸门等纯逻辑在无 langchain 环境下可测
ChatOpenAI = Any

_INSTRUCTION = '从基建运维故障复盘中抽取结构化故障要素，输出 JSON。文档未提及的字段填 null，不要推测。'
_COMPARABLE_FIELDS = ('severity', 'start_time', 'root_cause.category', 'change.type')
_SCHEMA_PATH = BASE_DIR / 'train' / 'schema' / 'fault_extract_schema.json'


def _get(data: dict[str, Any], path: str) -> Any:
    """按点号路径取嵌套字段，缺失返回 None。"""
    cur: Any = data
    for part in path.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def load_schema() -> dict[str, Any]:
    """读取 guided_json 约束文件；缺失返回空 dict（由调用方决定降级）。"""
    if not _SCHEMA_PATH.exists():
        logger.warning(f'抽取 schema 不存在: {_SCHEMA_PATH}')
        return {}
    with _SCHEMA_PATH.open(encoding='utf-8') as f:
        return json.load(f)


def build_client() -> Any:
    """用 vLLM 配置构造客户端；base_url/model 来自 .env，避免硬编码。"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=config.VLLM_EXTRACT_MODEL,
        api_key='EMPTY',
        base_url=config.VLLM_BASE_URL,
        temperature=0.2,
        max_tokens=512,
    )


async def llm_extract(doc: str, client: ChatOpenAI | None = None,
                      schema: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """单次抽取：调 vLLM 并解析 JSON，失败返回 None（不抛到主链路）。"""
    cli = client or build_client()
    constrained = schema if schema is not None else load_schema()
    try:
        # guided_json 经 extra_body 下传，由 vLLM 的 outlines 后端保证结构合法
        kwargs: dict[str, Any] = {'extra_body': {'guided_json': constrained}} if constrained else {}
        msg = await cli.ainvoke([
            ('system', _INSTRUCTION),
            ('human', doc),
        ], **kwargs)
        content = msg.content if isinstance(msg.content, str) else ''
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f'故障要素抽取 JSON 解析失败: {e}')
        return None
    except Exception as e:  # noqa: BLE001  模型不可达等异常只告警
        logger.warning(f'故障要素抽取调用失败: {e}')
        return None


def confidence_of(results: list[dict[str, Any]]) -> float:
    """按可比字段的采样一致率算置信度。

    自由文本（title/description/steps）三次采样表述必然不同，不进分母；
    恒为 null 的字段也不进分母，否则置信度会被系统性抬高。
    """
    valid = [r for r in results if r is not None]
    if not valid:
        return 0.0
    comparable = [f for f in _COMPARABLE_FIELDS
                  if any(_get(r, f) is not None for r in valid)]
    if not comparable:
        # 三次采样都没抽出可比字段，不能当满分；否则空结果会被闸门判 high 写进图
        return 0.0
    hits = sum(1 for f in comparable
               if len({json.dumps(_get(r, f), ensure_ascii=False, sort_keys=True) for r in valid}) == 1)
    return round(hits / len(comparable), 4)


async def extract_with_confidence(doc: str, samples: int = 3,
                                  client: ChatOpenAI | None = None,
                                  schema: dict[str, Any] | None = None
                                  ) -> tuple[dict[str, Any], float]:
    """同一份复盘采样 samples 次，返回（首份结果, 字段一致率）。"""
    cli = client or build_client()
    tasks = [llm_extract(doc, cli, schema) for _ in range(samples)]
    outs = await asyncio.gather(*tasks, return_exceptions=True)
    valid = [o for o in outs if isinstance(o, dict)]
    if not valid:
        return {}, 0.0
    return valid[0], confidence_of(valid)


def decide(data: dict[str, Any], confidence: float) -> str:
    """双闸门：high 自动入图 / review 人工复核 / drop 丢弃。"""
    if not data or confidence < 0.4:
        return 'drop'
    if confidence < 0.7:
        return 'review'
    return 'high'


def merge_conflict(structured: dict[str, Any], lora: dict[str, Any]) -> dict[str, Any]:
    """同字段冲突时结构化真值优先，LoRA 值记入 meta.lora_conflict 供审计。"""
    result = dict(structured)
    conflicts: dict[str, Any] = {}
    for key, lora_val in lora.items():
        base_val = structured.get(key)
        if base_val is not None and lora_val is not None and base_val != lora_val:
            conflicts[key] = lora_val
        elif base_val is None:
            result[key] = lora_val
    if conflicts:
        result['meta'] = {**structured.get('meta', {}), 'lora_conflict': conflicts}
    return result
