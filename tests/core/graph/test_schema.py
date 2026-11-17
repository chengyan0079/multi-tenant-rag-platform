"""graph.schema 单测：幂等 id、租户建边校验、ConfigItem 豁免。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph.schema import (
    EdgeType, GraphNode, NodeLabel, NodeSource,
    build_edge, check_edge_tenants, make_node_id,
)


def _node(label: NodeLabel, key: str, tenant_id: int | None) -> GraphNode:
    return GraphNode(label=label, key=key, tenant_id=tenant_id)


def test_make_node_id_is_stable_and_tenant_scoped():
    a = make_node_id(7, NodeLabel.FAULT, 'INC-1')
    assert a == make_node_id(7, NodeLabel.FAULT, 'INC-1')
    assert a != make_node_id(8, NodeLabel.FAULT, 'INC-1')
    assert a.startswith('t7_fault_')


def test_graph_node_id_property():
    node = _node(NodeLabel.CHANGE, 'CHG-1', 7)
    assert node.node_id == make_node_id(7, NodeLabel.CHANGE, 'CHG-1')


def test_check_edge_tenants_rejects_cross_tenant():
    a = _node(NodeLabel.FAULT, 'INC-1', 7)
    b = _node(NodeLabel.CHANGE, 'CHG-1', 8)
    try:
        check_edge_tenants(a, b)
    except ValueError as e:
        assert '跨租户' in str(e)
    else:
        raise AssertionError('应拒绝跨租户连边')


def test_check_edge_tenants_rejects_missing_tenant():
    a = _node(NodeLabel.FAULT, 'INC-1', None)
    b = _node(NodeLabel.CHANGE, 'CHG-1', 7)
    try:
        check_edge_tenants(a, b)
    except ValueError as e:
        assert 'tenant_id' in str(e)
    else:
        raise AssertionError('应拒绝缺失 tenant_id')


def test_build_edge_allows_config_item_without_tenant():
    change = _node(NodeLabel.CHANGE, 'CHG-1', 7)
    cfg = _node(NodeLabel.CONFIG_ITEM, 'POSTGRES_POOL_MAX_SIZE', None)
    edge = build_edge(change, cfg, EdgeType.MODIFIES, {'before': 16, 'after': 2})
    assert edge.tenant_id == 7
    assert edge.props['after'] == 2
    assert edge.start_id == change.node_id and edge.end_id == cfg.node_id


def test_build_edge_props_copied():
    change = _node(NodeLabel.CHANGE, 'CHG-1', 7)
    cfg = _node(NodeLabel.CONFIG_ITEM, 'K', None)
    src = {'lag_s': 210}
    edge = build_edge(change, cfg, EdgeType.MODIFIES, src)
    src['lag_s'] = 0
    assert edge.props['lag_s'] == 210


def test_node_source_default_structured():
    assert _node(NodeLabel.FAULT, 'x', 7).source == NodeSource.STRUCTURED
