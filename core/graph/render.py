"""图证据渲染：查询结果逐条转文本候选，包装成 RetrievedChunk 供现有 rerank 复用。"""
from __future__ import annotations

from typing import Any

from core.rag_retriever.retriever import RetrievedChunk

# 每条证据单独渲染成一行，rerank 才能逐条打分；格式：[标签] 时间 主语 谓词 值（来源）
_CHAIN_ID = 'causal_chain'


def _fmt(value: Any) -> str:
    return '' if value is None else str(value)


def render_evidence(qtype: str, rows: list[dict[str, Any]]) -> list[RetrievedChunk]:
    """按问题类型选渲染模板；每行一条候选，id 固定 0，来源标记写进 meta。"""
    out: list[RetrievedChunk] = []
    for row in rows:
        if qtype == 'rootcause':
            text = (
                f"[chg] {_fmt(row.get('change'))}：{_fmt(row.get('config_key'))} "
                f"{_fmt(row.get('before'))}->{_fmt(row.get('after'))}（变更单）\n"
                f"[pg]  pool_size={_fmt(row.get('pool_size'))} "
                f"pool_waiting={_fmt(row.get('pool_waiting'))}（连接池状态）\n"
                f"[reg] 实例 {_fmt(row.get('instance_id'))} removed @"
                f"{_fmt(row.get('status_time'))}，滞后 {_fmt(row.get('lag_s'))}s（Nacos）"
            )
        elif qtype == 'impact':
            faults = ', '.join(row.get('faults') or [])
            text = (
                f"[reg] 实例 {_fmt(row.get('instance_id'))} status={_fmt(row.get('status'))} "
                f"health={_fmt(row.get('health'))} @ {_fmt(row.get('status_time'))}（Nacos）"
                + (f"，关联故障：{faults}" if faults else '')
            )
        elif qtype == 'runbook':
            steps = row.get('runbook_steps') or []
            step_text = '；'.join(str(s) for s in steps) if isinstance(steps, list) else str(steps)
            text = (
                f"[sim] 相似事故 {_fmt(row.get('fault_id'))} {_fmt(row.get('title'))} "
                f"（置信 {_fmt(row.get('fault_conf'))}）\n"
                f"[rb]  {_fmt(row.get('runbook_title'))}：{step_text}（复盘）"
            )
        else:
            continue
        out.append(RetrievedChunk(
            chunk_id=0,
            content=text,
            doc_name='graph:causal_chain',
            score=0.0,
            meta={'origin': 'graph', 'chain_id': _CHAIN_ID,
                  'qtype': qtype, 'source': 'structured'},
        ))
    return out


def refill_chain(cands: list[RetrievedChunk], picked: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """rerank 命中任一链证据时补齐同链候选，避免因果链被切碎。"""
    picked_ids = {id(c) for c in picked}
    chains = {c.meta.get('chain_id') for c in picked if c.meta.get('chain_id')}
    if not chains:
        return picked
    extra = [c for c in cands
             if c.meta.get('chain_id') in chains and id(c) not in picked_ids]
    return picked + extra
