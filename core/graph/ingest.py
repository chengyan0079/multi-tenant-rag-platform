"""故障要素入图：LoRA 抽取结果 → 节点/边映射 → 闸门 → 统一写入。

并发采集/抽取只产出 GraphSnapshot，落库统一走 commit_snapshot，保证幂等与租户校验。
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

from common.config import config
from common.logger import logger, setup_logging
from core.graph.extractor_lora import decide, extract_with_confidence, merge_conflict
from core.graph.schema import (
    EdgeType, GraphNode, GraphSnapshot, NodeLabel, NodeSource,
)
from store.neo4j_store import Neo4jStore


@dataclass
class IngestResult:
    """一次入图结果；verdict=high 才真正写库，review/drop 只报告。"""

    verdict: str            # high / review / drop
    confidence: float
    nodes: int = 0
    edges: int = 0
    reason: str = ''


async def commit_snapshot(store: Neo4jStore, snap: GraphSnapshot) -> dict[str, int]:
    """统一写入口：先约束再节点再边（边 MATCH 需节点已存在）。"""
    await store.ensure_schema()
    await store.upsert_nodes(list(snap.nodes.values()))
    await store.upsert_edges(snap.edges)
    return {'nodes': len(snap.nodes), 'edges': len(snap.edges)}


def to_snapshot(data: dict[str, Any], tenant_id: int, confidence: float) -> GraphSnapshot:
    """把抽取 JSON 按 train/schema 字段映射成图节点/边。

    关系由字段位置决定（模型不产出关系名）；可比字段写进节点属性，供 Cypher 过滤。
    """
    snap = GraphSnapshot(tenant_id)
    fault_id = data.get('fault_id') or ''
    if not fault_id:
        return snap

    fault = snap.add(GraphNode(
        label=NodeLabel.FAULT, key=fault_id, tenant_id=tenant_id,
        source=NodeSource.LORA,
        props={
            'fault_id': fault_id,
            'title': data.get('title') or '',
            'severity': data.get('severity') or '',
            'start_time': data.get('start_time') or '',
            'confidence': confidence,
        },
    ))

    for symptom in data.get('symptoms') or []:
        if not isinstance(symptom, dict) or not symptom.get('name'):
            continue
        node = snap.add(GraphNode(
            label=NodeLabel.SYMPTOM, key=f'{fault_id}:{symptom["name"]}',
            tenant_id=tenant_id, source=NodeSource.LORA,
            props={'name': symptom['name'], 'observed_at': symptom.get('observed_at') or ''},
        ))
        snap.link(fault, node, EdgeType.EXHIBITS)

    change = data.get('change')
    if isinstance(change, dict) and change.get('content'):
        content = change['content']
        node = snap.add(GraphNode(
            label=NodeLabel.CHANGE, key=f'{fault_id}:{content}',
            tenant_id=tenant_id, source=NodeSource.LORA,
            props={
                'change_id': f'LORA-{fault_id}',
                'type': change.get('type') or '',
                'content': content,
                'time': change.get('time') or '',
            },
        ))
        snap.link(node, fault, EdgeType.TRIGGERED_BY,
                  {'lag_s': _lag_seconds(change.get('time'), data.get('start_time'))})

    root_cause = data.get('root_cause')
    if isinstance(root_cause, dict) and root_cause.get('category'):
        node = snap.add(GraphNode(
            label=NodeLabel.ROOT_CAUSE, key=f'{fault_id}:{root_cause["category"]}',
            tenant_id=tenant_id, source=NodeSource.LORA,
            props={
                'category': root_cause['category'],
                'description': root_cause.get('description') or '',
            },
        ))
        snap.link(fault, node, EdgeType.ROOT_CAUSE)

    runbook = data.get('runbook')
    if isinstance(runbook, dict) and runbook.get('title'):
        node = snap.add(GraphNode(
            label=NodeLabel.RUNBOOK, key=f'{fault_id}:{runbook["title"]}',
            tenant_id=tenant_id, source=NodeSource.LORA,
            props={
                'title': runbook['title'],
                'steps': runbook.get('steps') or [],
                'confidence': runbook.get('confidence') or 0.0,
            },
        ))
        snap.link(fault, node, EdgeType.RESOLVED_BY)

    return snap


def _lag_seconds(change_time: str | None, fault_time: str | None) -> int:
    """变更到故障的时间差（秒）；时间缺失或格式不对记 0，不阻断入图。"""
    if not change_time or not fault_time:
        return 0
    try:
        from datetime import datetime
        return max(0, int((datetime.fromisoformat(fault_time) -
                           datetime.fromisoformat(change_time)).total_seconds()))
    except ValueError:
        return 0


async def ingest_review(
    doc: str,
    tenant_id: int,
    store: Neo4jStore,
    client: Any = None,
    structured: dict[str, Any] | None = None,
) -> IngestResult:
    """抽取 + 闸门 + 冲突裁决 + 入图；review/drop 不写库，只报告。"""
    data, confidence = await extract_with_confidence(doc, client=client)
    verdict = decide(data, confidence)
    if verdict != 'high':
        return IngestResult(verdict=verdict, confidence=confidence,
                            reason='置信度未达自动入图阈值' if verdict == 'review' else '抽取为空或置信度低于 0.4')

    # 结构化真值优先：同字段冲突时保留直读值，LoRA 值记入 meta.lora_conflict
    effective = merge_conflict(structured or {}, data) if structured else data
    snap = to_snapshot(effective, tenant_id, confidence)
    conflicts = (effective.get('meta') or {}).get('lora_conflict') or {}
    if conflicts:
        for node in snap.nodes.values():
            if node.label == NodeLabel.FAULT:
                node.props['meta'] = {'lora_conflict': conflicts}
        logger.info(f'LoRA 与结构化字段冲突 {len(conflicts)} 项，保留结构化值: {list(conflicts)}')

    stats = await commit_snapshot(store, snap)
    similar = await link_similar_faults(store, tenant_id, data.get('fault_id', ''),
                                        (data.get('root_cause') or {}).get('category'))
    logger.info(f'故障要素入图完成 [tenant={tenant_id}, fault={data.get("fault_id")}]: '
                f'{stats}, similar+{similar}')
    return IngestResult(verdict=verdict, confidence=confidence,
                        nodes=stats['nodes'], edges=stats['edges'] + similar)


async def link_similar_faults(store: Neo4jStore, tenant_id: int,
                              fault_id: str, category: str | None) -> int:
    """按根因类别把新故障连到同租户的历史故障（SIMILAR_TO），供 runbook 查询召回。

    关系补全在图内完成，带 tenant_id 过滤防跨租户连边；category 为空则跳过。
    """
    if not fault_id or not category:
        return 0
    rows = await store.execute(
        'MATCH (f:Fault {tenant_id: $tid, fault_id: $fid}), '
        '(g:Fault {tenant_id: $tid})-[rc:ROOT_CAUSE]->(r:RootCause {category: $cat}) '
        'WHERE g.fault_id <> $fid '
        'WITH f, g ORDER BY g.start_time DESC LIMIT 3 '
        'MERGE (f)-[s:SIMILAR_TO]->(g) SET s.reason = $reason '
        'RETURN count(s) AS n',
        tid=tenant_id, fid=fault_id, cat=category,
        reason=f'同为 {category} 类根因',
    )
    return int(rows[0]['n']) if rows else 0


def main() -> None:
    """CLI：python -m core.graph.ingest --file 复盘.txt --tenant 100。"""
    parser = argparse.ArgumentParser(description='从故障复盘文本抽取要素并入图')
    parser.add_argument('--file', required=True, help='复盘文本文件')
    parser.add_argument('--tenant', type=int, default=config.DEFAULT_TENANT_ID)
    args = parser.parse_args()

    setup_logging()
    with open(args.file, encoding='utf-8') as f:
        doc = f.read()

    async def run() -> None:
        store = Neo4jStore()
        await store.connect()
        try:
            result = await ingest_review(doc, args.tenant, store)
            print(f'入图结果: {result}')
        finally:
            await store.close()

    asyncio.run(run())


if __name__ == '__main__':
    main()
