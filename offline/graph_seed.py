"""种子的图数据：INC-MVP-001 完整四跳链 + 2 条历史事故，供图路联调与验收。

run: python offline/graph_seed.py --tenant 100
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config
from common.logger import setup_logging
from core.graph.schema import (
    EdgeType, GraphEdge, GraphNode, NodeLabel, NodeSource, build_edge,
)
from store.neo4j_store import Neo4jStore

T0 = datetime(2026, 11, 17, 9, 0, 0)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def seed_nodes(tenant_id: int) -> tuple[list[GraphNode], list[GraphEdge]]:
    """构造种子节点与边；属性与 Cypher 字段对齐（change.time / ri.status_time 等）。"""
    change = GraphNode(
        label=NodeLabel.CHANGE, key='CHG-21', tenant_id=tenant_id,
        props={'change_id': 'CHG-21', 'type': 'config',
               'content': 'POSTGRES_POOL_MAX_SIZE 16->2', 'time': _at(0),
               'source': NodeSource.STRUCTURED.value},
    )
    cfg = GraphNode(
        label=NodeLabel.CONFIG_ITEM, key='POSTGRES_POOL_MAX_SIZE', tenant_id=None,
        props={'key': 'POSTGRES_POOL_MAX_SIZE', 'scope': 'nacos-SYSTEM'},
    )
    pg = GraphNode(
        label=NodeLabel.PG_INSTANCE, key=f'{config.POSTGRES_DATABASE}-{tenant_id}',
        tenant_id=tenant_id,
        props={'db_name': config.POSTGRES_DATABASE, 'pool_size': 2, 'pool_waiting': 37},
    )
    ri = GraphNode(
        label=NodeLabel.REGISTERED_INSTANCE, key='10.0.0.1:8800', tenant_id=tenant_id,
        props={'instance_id': '10.0.0.1:8800', 'ip': '10.0.0.1', 'port': 8800,
               'health': False, 'status': 'removed', 'status_time': _at(3.5)},
    )
    fault = GraphNode(
        label=NodeLabel.FAULT, key='INC-MVP-001', tenant_id=tenant_id,
        props={'fault_id': 'INC-MVP-001', 'title': '注册实例被摘除', 'severity': 'P1',
               'start_time': _at(3.5), 'confidence': 0.86,
               'root_cause.category': 'capacity',
               'source': NodeSource.LORA.value},
    )
    runbook = GraphNode(
        label=NodeLabel.RUNBOOK, key='RB-POOL-RECOVER', tenant_id=tenant_id,
        props={'title': '连接池容量回滚', 'steps': ['回滚连接池上限至 16', '重启被摘除实例'],
               'confidence': 0.91},
    )
    # 2 条历史事故：供 Q_SIMILAR 的 SIMILAR_TO 命中；AFFECTS 让影响面查询无告警
    hist1 = GraphNode(
        label=NodeLabel.FAULT, key='INC-HIST-001', tenant_id=tenant_id,
        props={'fault_id': 'INC-HIST-001', 'title': '连接池上限调小致实例被摘',
               'start_time': _at(-10080), 'confidence': 0.81},
    )
    hist2 = GraphNode(
        label=NodeLabel.FAULT, key='INC-HIST-002', tenant_id=tenant_id,
        props={'fault_id': 'INC-HIST-002', 'title': '连接配额耗尽',
               'start_time': _at(-20160), 'confidence': 0.79},
    )
    # RootCause 节点：与 LoRA 入图形态一致，供 link_similar_faults 按类别连相似边
    rc = GraphNode(
        label=NodeLabel.ROOT_CAUSE, key=f'{tenant_id}:capacity', tenant_id=tenant_id,
        props={'category': 'capacity', 'description': '连接池容量不足'},
    )
    node_list = [change, cfg, pg, ri, fault, runbook, hist1, hist2, rc]

    edges = [
        build_edge(change, cfg, EdgeType.MODIFIES, {'before': 16, 'after': 2}),
        build_edge(cfg, pg, EdgeType.APPLIES_TO),
        build_edge(ri, pg, EdgeType.DEPENDS_ON),
        build_edge(change, fault, EdgeType.TRIGGERED_BY, {'lag_s': 210}),
        build_edge(fault, runbook, EdgeType.RESOLVED_BY),
        build_edge(fault, ri, EdgeType.AFFECTS),
        build_edge(hist1, ri, EdgeType.AFFECTS),
        build_edge(hist1, rc, EdgeType.ROOT_CAUSE),
        build_edge(hist2, rc, EdgeType.ROOT_CAUSE),
        build_edge(fault, hist1, EdgeType.SIMILAR_TO, {'reason': '同为连接池容量问题'}),
        build_edge(fault, hist2, EdgeType.SIMILAR_TO, {'reason': '同属容量耗尽类'}),
    ]
    return node_list, edges


async def seed(tenant_id: int) -> dict[str, int]:
    store = Neo4jStore()
    from core.graph.ingest import commit_snapshot
    from core.graph.schema import GraphSnapshot
    await store.connect()
    try:
        nodes, edges = seed_nodes(tenant_id)
        return await commit_snapshot(store, GraphSnapshot(tenant_id,
                                                          {n.node_id: n for n in nodes}, edges))
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='写入故障图谱种子数据')
    parser.add_argument('--tenant', type=int, default=config.DEFAULT_TENANT_ID)
    args = parser.parse_args()
    setup_logging()
    stats = asyncio.run(seed(args.tenant))
    print(f'种子数据写入完成: {stats}')


if __name__ == '__main__':
    main()
