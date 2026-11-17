"""种子数据单测：四跳链完整、租户校验、边字段对齐 Cypher。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from offline.graph_seed import seed_nodes


def test_seed_has_four_hop_chain():
    nodes, edges = seed_nodes(7)
    keys = {n.key for n in nodes}
    assert 'CHG-21' in keys and 'INC-MVP-001' in keys
    types = [e.edge_type.value for e in edges]
    assert {'MODIFIES', 'APPLIES_TO', 'DEPENDS_ON', 'TRIGGERED_BY',
            'RESOLVED_BY', 'AFFECTS', 'SIMILAR_TO'} <= set(types)


def test_seed_edges_tenant_safe():
    nodes, edges = seed_nodes(7)
    by_id = {n.node_id: n for n in nodes}
    for e in edges:
        a, b = by_id[e.start_id], by_id[e.end_id]
        ta, tb = a.tenant_id, b.tenant_id
        assert ta is None or tb is None or ta == tb


def test_seed_cypher_fields_present():
    nodes, _ = seed_nodes(7)
    props = {n.key: n.props for n in nodes}
    assert props['CHG-21']['time']
    assert props['10.0.0.1:8800']['status_time']
    assert props['10.0.0.1:8800']['status'] == 'removed'
    assert props['INC-MVP-001']['fault_id'] == 'INC-MVP-001'
