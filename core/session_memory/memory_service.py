"""会话记忆服务：compaction（LLM 增量压缩）+ 注入文本构造。

对齐 Octop 的 octop_memory.core.Memory 的 compacting 阶段：每若干轮由 LLM 把
"旧记忆 + 最近对话"压缩成新的 summary + facts，写回存储。调用方（SessionService）
负责节流触发；本服务只做压缩与注入文本生成。
"""
import json
import re
from typing import Any

from common.logger import logger
from store.memory_store import MemoryStore, SessionMemory
from core.rag_model.llm_client import acomplete


_COMPACT_PROMPT = (
    "你是一个对话记忆压缩器。下面是某会话已有的记忆（summary/facts）和最近几轮对话。"
    "请输出更新后的记忆，必须是严格 JSON：{{\"summary\": 字符串, \"facts\": 字符串数组}}。"
    "summary 用一两句话概括会话主旨与关键结论；facts 抽取用户偏好、关键实体、待办等结构化事实，"
    "去重合并历史 facts 与本轮新事实，不要丢失重要信息。\n\n"
    "已有记忆：\n{existing}\n\n最近对话：\n{recent}\n\n只输出 JSON。"
)


class MemoryService:
    def __init__(self, memory_store: MemoryStore | None = None,
                 session_store: Any | None = None) -> None:
        self.store = memory_store or MemoryStore()
        self.session_store = session_store  # 可选，用于自动取最近历史

    async def get(self, user_id: str, session_id: str) -> SessionMemory:
        return await self.store.get(user_id, session_id)

    async def delete(self, user_id: str, session_id: str) -> None:
        """删除会话窗口记忆（清空压缩上下文）。"""
        await self.store.delete(user_id, session_id)

    async def compact(self, user_id: str, session_id: str,
                      recent_history: list[dict[str, Any]] | None = None,
                      recent_turns: int = 8) -> SessionMemory:
        """LLM 增量压缩记忆：合并旧记忆与最近对话，写回存储并返回新记忆。"""
        if recent_history is None and self.session_store is not None:
            recent_history = await self.session_store.get_history(
                user_id, session_id, last=recent_turns)
        existing = await self.store.get(user_id, session_id)
        recent_text = "\n".join(
            f"{h.get('role')}: {h.get('content')}" for h in (recent_history or []))
        existing_text = f"summary: {existing.summary}\nfacts: {', '.join(existing.facts)}"
        prompt = _COMPACT_PROMPT.format(existing=existing_text, recent=recent_text)
        raw = await acomplete(prompt, temperature=0.0, max_tokens=1024)
        new_mem = self._parse(raw, existing)
        await self.store.save(user_id, session_id, new_mem)
        return new_mem

    @staticmethod
    def _parse(raw: str, existing: SessionMemory) -> SessionMemory:
        """解析 LLM 返回的 JSON；容错：失败则保留旧记忆。"""
        if not raw:
            return existing
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                return existing
            try:
                data = json.loads(m.group(0))
            except Exception:
                return existing
        if not isinstance(data, dict):
            return existing
        summary = (data.get('summary') or '').strip() or existing.summary
        facts = data.get('facts') or []
        if not isinstance(facts, list):
            facts = [str(facts)]
        merged = list(existing.facts)
        for f in facts:
            f = str(f).strip()
            if f and f not in merged:
                merged.append(f)
        return SessionMemory(summary=summary, facts=merged)

    @staticmethod
    def build_injection_text(memory: SessionMemory) -> str:
        """把记忆拼成注入生成器的文本；无记忆返回空串。"""
        if not memory or (not memory.summary and not memory.facts):
            return ''
        lines = ['[会话记忆]']
        if memory.summary:
            lines.append(f"摘要: {memory.summary}")
        if memory.facts:
            lines.append("已知事实:")
            lines.extend(f"- {f}" for f in memory.facts)
        return "\n".join(lines)
