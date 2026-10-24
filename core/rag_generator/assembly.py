"""多轮上下文装配：把记忆 / 历史 / 当前问题按分层配额拼成送进模板的文本。

设计方案见 docs/上下文压缩设计方案.md §4，要点：
- 记忆独立成块，不再混进"当前问题"标签下（修语义错位）；
- 只有 P2（历史）与 P3（记忆）可裁剪，检索片段（P0）不在这里动，
  它由 core.qa_router.compression 负责，且只在 chunks 自身超窗时才启用；
- 裁剪顺序固定为：冗余去重（无损）→ 截取 history → 压缩 memory。
"""
import re
from typing import Any

from common.config import config
from common.logger import logger

# 句边界：与 core/rag_indexer/semantic_splitter.py 保持一致，截断时避免把句子切一半
_SENT_SPLIT_RE = re.compile(r'(?<=[。！？\n])')

# 判断"某轮历史是否已被记忆覆盖"的采样窗口（字符）与命中率阈值。
# 记忆是 LLM 生成的摘要，措辞不会与原文逐字一致，所以用片段包含关系做近似，而非全等比较。
_COVERAGE_WINDOW = 12
_COVERAGE_THRESHOLD = 0.6

_TRUNCATE_SUFFIX = '…（已截断）'


def split_sentences(text: str) -> list[str]:
    """按句边界切分文本（保留标点），供按句截断使用。"""
    if not text:
        return []
    return [s for s in _SENT_SPLIT_RE.split(text) if s]


def truncate_by_sentence(text: str, limit: int, suffix: str = _TRUNCATE_SUFFIX) -> str:
    """截到不超过 limit 的最后一个完整句；首句就超限才按字符硬切。"""
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    kept: list[str] = []
    total = 0
    for sentence in split_sentences(text):
        if total + len(sentence) > limit:
            break
        kept.append(sentence)
        total += len(sentence)
    if not kept:
        return text[:limit] + suffix
    return ''.join(kept).rstrip() + suffix


def _normalize(text: str) -> str:
    """去掉空白，便于做包含判断（记忆与原文的换行/空格差异不应影响重合度）。"""
    return re.sub(r'\s+', '', text or '')


def coverage_ratio(text: str, memory: str, window: int = _COVERAGE_WINDOW) -> float:
    """估算 text 被 memory 覆盖的比例：按固定长度窗口采样，统计命中率。"""
    if not text or not memory:
        return 0.0
    t, m = _normalize(text), _normalize(memory)
    if len(t) <= window:
        return 1.0 if t in m else 0.0
    hit = total = 0
    for i in range(0, len(t) - window + 1, window):
        total += 1
        if t[i:i + window] in m:
            hit += 1
    return hit / total if total else 0.0


def format_history(history: list[dict[str, Any]]) -> str:
    """把历史消息拼成 `role: content` 形式。"""
    return "\n".join(f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history)


def drop_covered_by_memory(
    history: list[dict[str, Any]],
    memory_text: str,
    threshold: float = _COVERAGE_THRESHOLD,
    keep_last: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """丢掉已被记忆覆盖的旧轮次（无损去重），返回 (剩余历史, 去掉条数)。

    最近的 keep_last 条不动：记忆是历史压缩出来的，最近几轮通常还没进记忆，
    而且指代消解（"它""那个方案"）主要依赖最近的消息。
    """
    if not memory_text or not history:
        return list(history), 0
    protected = max(0, len(history) - keep_last)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for idx, item in enumerate(history):
        if idx < protected and coverage_ratio(str(item.get('content', '')), memory_text) >= threshold:
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def limit_memory(memory_text: str, max_chars: int) -> tuple[str, bool]:
    """把记忆文本压到 max_chars 以内，返回 (文本, 是否被截断)。"""
    if max_chars <= 0:
        return memory_text, False
    if len(memory_text) <= max_chars:
        return memory_text, False
    return truncate_by_sentence(memory_text, max_chars), True


def assemble_question(
    query: str,
    history: list[dict[str, Any]] | None,
    memory_text: str,
    context_chars: int = 0,
    template_chars: int = 0,
) -> tuple[str, dict[str, Any]]:
    """按分层配额装配"记忆 / 历史 / 当前问题"，返回 (拼好的文本, 过程统计)。

    额度模型（设计方案 §4.2）：总额度先扣掉模板、检索上下文与当前问题的实际占用，
    剩余部分才分给记忆与历史，这样检索证据天然不会被记忆和历史挤掉。
    """
    items = list(history or [])
    memory_text = memory_text or ''
    stats: dict[str, Any] = {
        'history_in': len(items),
        'dropped_by_dedup': 0,
        'history_kept': 0,
        'memory_truncated': False,
        'chars_before': 0,
        'chars_after': 0,
    }

    # 1) 历史先按配置窗口取最近若干条
    max_messages = config.HISTORY_MAX_MESSAGES
    if max_messages > 0 and len(items) > max_messages:
        items = items[-max_messages:]

    # 2) 记忆先按配置限长（它已是压缩态，正常情况下这一步就够）
    memory_text, memory_truncated = limit_memory(memory_text, config.MEMORY_MAX_CHARS)

    # 3) 配额计算：P0（模板 + 检索上下文 + 当前问题）先占位，剩下的才是 P2/P3 的额度
    fixed = template_chars + context_chars + len(query)
    budget = max(0, config.ASSEMBLY_MAX_PROMPT_CHARS - fixed)
    stats['chars_before'] = len(memory_text) + len(format_history(items)) + fixed
    used = len(memory_text) + len(format_history(items))

    # 3.1 无损去重：记忆已覆盖的旧轮次不再重复塞原文
    if used > budget:
        items, dropped = drop_covered_by_memory(items, memory_text)
        stats['dropped_by_dedup'] = dropped
        used = len(memory_text) + len(format_history(items))

    # 3.2 截取历史：只保留最近的，但不低于下限（保住指代消解需要的上下文）
    floor = config.ASSEMBLY_MIN_HISTORY_MESSAGES
    while len(items) > floor and used > budget:
        items = items[1:]
        used = len(memory_text) + len(format_history(items))

    # 3.3 最后手段：把记忆再压一次，压到历史腾出来的剩余额度内
    if used > budget and memory_text:
        remain = max(0, budget - len(format_history(items)))
        memory_text, truncated = limit_memory(memory_text, remain)
        memory_truncated = memory_truncated or truncated

    stats['history_kept'] = len(items)
    stats['memory_truncated'] = memory_truncated
    stats['chars_after'] = len(memory_text) + len(format_history(items)) + fixed

    if stats['dropped_by_dedup'] or memory_truncated or stats['history_kept'] < stats['history_in']:
        logger.info(
            '多轮上下文裁剪 [history=%d→%d, 去重=%d, 记忆截断=%s, chars=%d→%d]'
            % (stats['history_in'], stats['history_kept'], stats['dropped_by_dedup'],
               memory_truncated, stats['chars_before'], stats['chars_after'])
        )

    blocks: list[str] = []
    if memory_text:
        blocks.append(f"长期记忆:\n{memory_text}")
    if items:
        blocks.append(f"历史对话:\n{format_history(items)}")
    blocks.append(f"当前问题:\n{query}")
    return "\n\n".join(blocks), stats
