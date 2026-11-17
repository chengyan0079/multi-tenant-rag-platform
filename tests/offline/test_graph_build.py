"""graph_build 单测：session / Nacos client 全部 mock，验证直读采集与快照组装。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from offline import graph_build as gb

# SQL 文本构造在测试里换成透传函数，避免依赖 sqlalchemy 安装；
# 只 patch 本模块符号，不污染全局 sys.modules。
gb._text = lambda sql: sql


def _run(coro):
    return asyncio.run(coro)


def _session_returning(row):
    session = mock.MagicMock()
    execute_result = mock.MagicMock()
    execute_result.one.return_value = row
    execute_result.all.return_value = [row]
    session.execute = mock.AsyncMock(return_value=execute_result)
    return session


def test_collect_api_symptoms_writes_symptom_with_ratio():
    row = mock.MagicMock(total=10, bad=4)
    session = _session_returning(row)
    snap = _run(gb.collect_api_symptoms(session, 7))
    assert len(snap.nodes) == 1
    node = next(iter(snap.nodes.values()))
    assert node.label.value == 'Symptom'
    assert node.props['name'] == '5xx 占比 40%'
    assert node.props['metric_source'] == 'log_agg'


def test_collect_api_symptoms_skip_when_no_traffic():
    session = _session_returning(mock.MagicMock(total=0, bad=0))
    snap = _run(gb.collect_api_symptoms(session, 7))
    assert snap.nodes == {}


def test_collect_instance_state_marks_removed():
    async def fake_list():
        return [
            {'ip': '10.0.0.1', 'port': 8800, 'healthy': True, 'enabled': True},
            {'ip': '10.0.0.2', 'port': 8800, 'healthy': False, 'enabled': True},
        ]
    snap = _run(gb.collect_instance_state(7, fake_list))
    assert len(snap.nodes) == 2
    statuses = {n.props['status'] for n in snap.nodes.values()}
    assert statuses == {'up', 'removed'}


def test_collect_config_requests_writes_change():
    row = mock.MagicMock(id=21, data_id='K', title='pool 16->2', created_at=None)
    session = _session_returning(row)
    snap = _run(gb.collect_config_requests(session, 7))
    assert len(snap.nodes) == 1
    node = next(iter(snap.nodes.values()))
    assert node.key == 'CHG-21'
    assert node.props['source'] == 'structured'


def test_collect_nacos_changes_builds_modifies_edge():
    client = mock.MagicMock()
    client.list_configs = mock.MagicMock(return_value={
        'data': {'configItems': [{'dataId': 'POSTGRES_POOL_MAX_SIZE'}]},
    })
    snap = _run(gb.collect_nacos_changes(client, 7))
    assert len(snap.nodes) == 2      # Change + ConfigItem
    assert len(snap.edges) == 1
    assert snap.edges[0].edge_type.value == 'MODIFIES'
    assert snap.edges[0].tenant_id == 7


def test_snapshot_link_rejects_cross_tenant():
    from core.graph.schema import GraphNode, NodeLabel
    snap = gb.GraphSnapshot(7)
    a = GraphNode(label=NodeLabel.FAULT, key='a', tenant_id=7)
    b = GraphNode(label=NodeLabel.CHANGE, key='b', tenant_id=8)
    try:
        snap.link(a, b, mock.MagicMock())
    except ValueError:
        pass
    else:
        raise AssertionError('应拒绝跨租户连边')


def test_build_graph_upserts_and_returns_stats():
    store = mock.MagicMock()
    store.ensure_schema = mock.AsyncMock()
    store.upsert_nodes = mock.AsyncMock()
    store.upsert_edges = mock.AsyncMock()
    session = _session_returning(mock.MagicMock(total=1, bad=0))
    engine = mock.MagicMock()
    engine.pool = mock.MagicMock()
    engine.pool.status = mock.MagicMock(return_value='Size:16 Checkedout:2 Overflow:0')

    async def fake_list():
        return []

    stats = _run(gb.build_graph(7, store, session, engine, fake_list))
    assert stats['nodes'] >= 2
    store.upsert_nodes.assert_awaited_once()
    store.upsert_edges.assert_awaited_once()


def test_nacos_instance_lister_maps_model_dump():
    inst = mock.MagicMock()
    inst.model_dump.return_value = {'ip': '10.0.0.1', 'port': 8800,
                                    'healthy': True, 'enabled': True}
    naming = mock.MagicMock()
    naming.list_instances = mock.AsyncMock(return_value=[inst])
    naming.shutdown = mock.AsyncMock()
    fake = mock.MagicMock()
    fake.ClientConfig = mock.MagicMock()
    fake.ListInstanceParam = mock.MagicMock()
    fake.NacosNamingService = mock.MagicMock()
    fake.NacosNamingService.create_naming_service = mock.AsyncMock(return_value=naming)
    with mock.patch.dict(sys.modules, {'v2.nacos': fake}):
        lister = _run(gb._make_nacos_instance_lister())
        result = _run(lister())
    assert result == [{'ip': '10.0.0.1', 'port': 8800, 'healthy': True, 'enabled': True}]
    naming.shutdown.assert_awaited_once()


def test_make_nacos_config_client_delegates():
    sentinel = object()
    with mock.patch('core.config_center.nacos_config.get_config_center_client',
                    return_value=sentinel):
        assert gb._make_nacos_config_client() is sentinel

