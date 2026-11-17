"""入图单测：扁平 JSON 映射、闸门、冲突裁决、统一写入。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import ingest as ig
from core.graph.schema import EdgeType, NodeLabel, NodeSource


def _run(coro):
    return asyncio.run(coro)


def _sample_data():
    return {
        'fault_id': 'INC-TEST-001',
        'title': '连接池上限误调',
        'severity': 'P1',
        'start_time': '2026-11-17T10:00:00',
        'symptoms': [{'name': '5xx 占比 43%', 'observed_at': '2026-11-17T10:05:00'}],
        'change': {'type': 'config', 'content': 'POOL_MAX 16->2',
                   'time': '2026-11-17T09:56:30'},
        'root_cause': {'category': 'capacity', 'description': '连接池打满'},
        'runbook': {'title': '回滚连接池', 'steps': ['回滚', '重启'], 'confidence': 0.9},
    }


def test_to_snapshot_maps_all_entities_and_edges():
    snap = ig.to_snapshot(_sample_data(), 7, 0.85)
    labels = {n.label for n in snap.nodes.values()}
    assert labels == {NodeLabel.FAULT, NodeLabel.SYMPTOM, NodeLabel.CHANGE,
                      NodeLabel.ROOT_CAUSE, NodeLabel.RUNBOOK}
    assert {e.edge_type for e in snap.edges} == {
        EdgeType.EXHIBITS, EdgeType.TRIGGERED_BY,
        EdgeType.ROOT_CAUSE, EdgeType.RESOLVED_BY,
    }
    assert all(n.tenant_id == 7 and n.source == NodeSource.LORA for n in snap.nodes.values())


def test_to_snapshot_computes_lag_seconds():
    snap = ig.to_snapshot(_sample_data(), 7, 0.85)
    edges = [e for e in snap.edges if e.edge_type == EdgeType.TRIGGERED_BY]
    assert edges[0].props['lag_s'] == 210


def test_to_snapshot_empty_fault_id_returns_empty():
    assert ig.to_snapshot({'title': 'x'}, 7, 0.9).nodes == {}


def test_lag_seconds_invalid_returns_zero():
    assert ig._lag_seconds('bad', 'also-bad') == 0
    assert ig._lag_seconds(None, 'x') == 0


def test_commit_snapshot_calls_in_order():
    store = mock.MagicMock()
    store.ensure_schema = mock.AsyncMock()
    store.upsert_nodes = mock.AsyncMock()
    store.upsert_edges = mock.AsyncMock()
    snap = ig.to_snapshot(_sample_data(), 7, 0.85)
    stats = _run(ig.commit_snapshot(store, snap))
    assert stats['nodes'] == 5 and stats['edges'] == 4
    assert store.ensure_schema.await_count == 1
    assert store.upsert_nodes.await_count == 1
    assert store.upsert_edges.await_count == 1


def test_ingest_review_high_writes():
    with mock.patch.object(ig, 'extract_with_confidence', new=mock.AsyncMock(
            return_value=(_sample_data(), 0.85))):
        store = mock.MagicMock()
        store.ensure_schema = mock.AsyncMock()
        store.upsert_nodes = mock.AsyncMock()
        store.upsert_edges = mock.AsyncMock()
        store.execute = mock.AsyncMock(return_value=[{'n': 2}])
        result = _run(ig.ingest_review('复盘', 7, store))
    assert result.verdict == 'high' and result.nodes == 5 and result.edges == 6
    store.upsert_nodes.assert_awaited_once()


def test_ingest_review_review_does_not_write():
    with mock.patch.object(ig, 'extract_with_confidence', new=mock.AsyncMock(
            return_value=(_sample_data(), 0.5))):
        store = mock.MagicMock()
        store.upsert_nodes = mock.AsyncMock()
        result = _run(ig.ingest_review('复盘', 7, store))
    assert result.verdict == 'review' and result.nodes == 0
    store.upsert_nodes.assert_not_awaited()


def test_link_similar_faults_skips_without_category():
    store = mock.MagicMock()
    store.execute = mock.AsyncMock()
    assert _run(ig.link_similar_faults(store, 7, 'INC-1', None)) == 0
    store.execute.assert_not_awaited()


def test_link_similar_faults_filters_tenant():
    store = mock.MagicMock()
    store.execute = mock.AsyncMock(return_value=[{'n': 3}])
    assert _run(ig.link_similar_faults(store, 7, 'INC-1', 'capacity')) == 3
    kwargs = store.execute.await_args.kwargs
    assert kwargs['tid'] == 7 and kwargs['cat'] == 'capacity'
    assert 'tenant_id: $tid' in store.execute.await_args.args[0]


def test_ingest_review_structured_wins_on_conflict():
    structured = {'change': {'content': '结构化真值', 'type': 'config'}}
    with mock.patch.object(ig, 'extract_with_confidence', new=mock.AsyncMock(
            return_value=(_sample_data(), 0.9))):
        store = mock.MagicMock()
        store.ensure_schema = mock.AsyncMock()
        store.upsert_nodes = mock.AsyncMock()
        store.upsert_edges = mock.AsyncMock()
        store.execute = mock.AsyncMock(return_value=[{'n': 0}])
        result = _run(ig.ingest_review('复盘', 7, store, structured=structured))
    assert result.verdict == 'high'
    nodes = store.upsert_nodes.await_args.args[0]
    change = [n for n in nodes if n.label == NodeLabel.CHANGE][0]
    assert change.props['content'] == '结构化真值'   # 结构化优先
    fault = [n for n in nodes if n.label == NodeLabel.FAULT][0]
    assert 'lora_conflict' in fault.props['meta']    # LoRA 值留痕
