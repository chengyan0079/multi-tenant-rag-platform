"""Neo4jStore 单测：驱动全部 mock，验证 upsert 语句与幂等语义。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph.schema import EdgeType, GraphEdge, GraphNode, NodeLabel
from store import neo4j_store as ns


def _run(coro):
    return asyncio.run(coro)


def _store_with_session():
    """构造一个 driver mock，session 支持 async with。"""
    session = mock.MagicMock()
    session.run = mock.AsyncMock()
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=session)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    driver = mock.MagicMock()
    driver.session = mock.MagicMock(return_value=ctx)
    return ns.Neo4jStore(driver), session


def test_upsert_nodes_empty_no_session():
    store, session = _store_with_session()
    assert _run(store.upsert_nodes([])) == 0
    session.run.assert_not_awaited()


def test_upsert_nodes_writes_id_and_source():
    store, session = _store_with_session()
    node = GraphNode(label=NodeLabel.FAULT, key='INC-1', tenant_id=7,
                     props={'title': 'x'})
    assert _run(store.upsert_nodes([node])) == 1
    cypher = session.run.await_args.args[0]
    kwargs = session.run.await_args.kwargs
    assert 'MERGE (n:Fault' in cypher
    assert kwargs['id'] == node.node_id
    assert kwargs['props']['source'] == 'structured'
    assert kwargs['props']['tenant_id'] == 7


def test_upsert_edges_matches_both_endpoints():
    store, session = _store_with_session()
    edge = GraphEdge(edge_type=EdgeType.MODIFIES, start_id='a', end_id='b',
                     tenant_id=7, props={'before': 16})
    assert _run(store.upsert_edges([edge])) == 1
    cypher = session.run.await_args.args[0]
    kwargs = session.run.await_args.kwargs
    assert 'MATCH (a {id: $start_id}), (b {id: $end_id})' in cypher
    assert 'MERGE (a)-[r:MODIFIES]->(b)' in cypher
    assert kwargs['props']['before'] == 16


def test_upsert_edges_with_labels_matches_by_label():
    store, session = _store_with_session()
    edge = GraphEdge(edge_type=EdgeType.MODIFIES, start_id='a', end_id='b',
                     tenant_id=7, start_label=NodeLabel.CHANGE,
                     end_label=NodeLabel.CONFIG_ITEM)
    _run(store.upsert_edges([edge]))
    cypher = session.run.await_args.args[0]
    assert 'MATCH (a:Change {id: $start_id}), (b:ConfigItem {id: $end_id})' in cypher


def test_upsert_without_driver_raises():
    store = ns.Neo4jStore()
    try:
        _run(store.upsert_nodes([GraphNode(label=NodeLabel.FAULT, key='x')]))
    except RuntimeError as e:
        assert 'connect' in str(e)
    else:
        raise AssertionError('未连接应抛错')


def test_query_returns_data():
    store, session = _store_with_session()
    result = mock.MagicMock()
    result.data = mock.AsyncMock(return_value=[{'x': 1}])
    session.run = mock.AsyncMock(return_value=result)
    rows = _run(store.query('RETURN 1 AS x', tid=7))
    assert rows == [{'x': 1}]
    session.run.assert_awaited_once_with('RETURN 1 AS x', tid=7)


def test_ensure_schema_creates_constraints_and_indexes():
    store, session = _store_with_session()
    _run(store.ensure_schema())
    statements = [c.args[0] for c in session.run.await_args_list]
    assert any('CONSTRAINT' in s for s in statements)
    assert any('INDEX' in s for s in statements)
