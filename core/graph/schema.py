"""故障图谱 schema：8 类节点 / 9 类边、幂等 id、建边租户校验、数据来源枚举。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeLabel(str, Enum):
    """节点标签；与 Neo4j 里的 label 同名。"""

    FAULT = 'Fault'
    SYMPTOM = 'Symptom'
    CHANGE = 'Change'
    CONFIG_ITEM = 'ConfigItem'
    PG_INSTANCE = 'PgInstance'
    REGISTERED_INSTANCE = 'RegisteredInstance'
    ROOT_CAUSE = 'RootCause'
    RUNBOOK = 'Runbook'


class EdgeType(str, Enum):
    """边类型；关系由字段位置唯一决定，模型不产出关系名。"""

    MODIFIES = 'MODIFIES'
    APPLIES_TO = 'APPLIES_TO'
    DEPENDS_ON = 'DEPENDS_ON'
    EXHIBITS = 'EXHIBITS'
    AFFECTS = 'AFFECTS'
    ROOT_CAUSE = 'ROOT_CAUSE'
    TRIGGERED_BY = 'TRIGGERED_BY'
    RESOLVED_BY = 'RESOLVED_BY'
    SIMILAR_TO = 'SIMILAR_TO'


class NodeSource(str, Enum):
    """双路线标记：结构化接口直读 / LoRA 语义抽取。"""

    STRUCTURED = 'structured'
    LORA = 'lora'


# ConfigItem 是系统级配置（rag_config_change_request 无 tenant_id），豁免租户校验
TENANT_SCOPED_LABELS: frozenset[NodeLabel] = frozenset({
    NodeLabel.FAULT, NodeLabel.SYMPTOM, NodeLabel.CHANGE,
    NodeLabel.PG_INSTANCE, NodeLabel.REGISTERED_INSTANCE,
    NodeLabel.ROOT_CAUSE, NodeLabel.RUNBOOK,
})


@dataclass
class GraphNode:
    """待写入的图节点：label + 业务键 + 属性。

    tenant_id 由调用方从上下文注入，不从抽取结果里取；ConfigItem 传 None。
    """

    label: NodeLabel
    key: str                       # 业务唯一键（如变更单号 / 故障单号）
    props: dict[str, Any] = field(default_factory=dict)
    tenant_id: int | None = None
    source: NodeSource = NodeSource.STRUCTURED

    @property
    def node_id(self) -> str:
        return make_node_id(self.tenant_id, self.label, self.key)


@dataclass
class GraphEdge:
    """待写入的图边：类型 + 两端节点 id + 可选属性。

    start_label/end_label 用于 MATCH 时限定标签，避免不同标签恰好同 id 时误连。
    """

    edge_type: EdgeType
    start_id: str
    end_id: str
    props: dict[str, Any] = field(default_factory=dict)
    tenant_id: int | None = None
    start_label: NodeLabel | None = None
    end_label: NodeLabel | None = None


@dataclass
class GraphSnapshot:
    """一次采集/抽取的增量：节点按 id 去重，边在构造时已做租户校验。

    直读采集器与 LoRA 抽取共用此容器，统一由 ingest.commit_snapshot 写入。
    """

    tenant_id: int
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)

    def add(self, node: GraphNode) -> GraphNode:
        self.nodes[node.node_id] = node
        return node

    def link(self, start: GraphNode, end: GraphNode, edge_type: EdgeType,
             props: dict[str, Any] | None = None) -> None:
        self.edges.append(build_edge(start, end, edge_type, props))


def make_node_id(tenant_id: int | None, label: NodeLabel, key: str) -> str:
    """内容 hash 生成幂等 id：相同输入恒定，重复 upsert 不产生新节点。"""
    prefix = f't{tenant_id}_' if tenant_id is not None else 'sys_'
    digest = hashlib.md5(f'{tenant_id}:{label.value}:{key}'.encode('utf-8')).hexdigest()[:8]
    return f'{prefix}{label.value.lower()}_{digest}'


def check_edge_tenants(start: GraphNode, end: GraphNode) -> None:
    """建边前校验两端租户一致，不一致直接拒绝（防跨租户连边）。

    ConfigItem 无 tenant_id，豁免校验；其余节点必须带 tenant_id。
    """
    for node in (start, end):
        if node.label in TENANT_SCOPED_LABELS and node.tenant_id is None:
            raise ValueError(f'{node.label.value} 缺少 tenant_id，拒绝建边')

    a = start.tenant_id
    b = end.tenant_id
    if a is not None and b is not None and a != b:
        raise ValueError(f'拒绝跨租户连边：{start.node_id}(t={a}) -> {end.node_id}(t={b})')


def build_edge(start: GraphNode, end: GraphNode, edge_type: EdgeType,
               props: dict[str, Any] | None = None) -> GraphEdge:
    """构造边并顺带完成租户校验，调用方不必单独记得调 check_edge_tenants。"""
    check_edge_tenants(start, end)
    # 两端租户一致时取任一端；ConfigItem 参与的边取非 None 那端
    tenant_id = start.tenant_id if start.tenant_id is not None else end.tenant_id
    return GraphEdge(
        edge_type=edge_type,
        start_id=start.node_id,
        end_id=end.node_id,
        props=dict(props or {}),
        tenant_id=tenant_id,
        start_label=start.label,
        end_label=end.label,
    )
