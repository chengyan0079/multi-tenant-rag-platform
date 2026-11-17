"""图查询单测：验证三条查询的租户参数与空类型降级。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import query as gq


def _run(coro):
    return asyncio.run(coro)


def test_graph_search_passes_tenant_and_params():
    store = mock.MagicMock()
    store.query = mock.AsyncMock(return_value=[{'x': 1}])
    rows = _run(gq.graph_search('rootcause', store, 7, fault_id='INC-1'))
    assert rows == [{'x': 1}]
    kwargs = store.query.await_args.kwargs
    assert kwargs['tid'] == 7 and kwargs['fault_id'] == 'INC-1'


def test_graph_search_unknown_type_returns_empty():
    store = mock.MagicMock()
    store.query = mock.AsyncMock()
    assert _run(gq.graph_search('unknown', store, 7)) == []
    store.query.assert_not_awaited()


def test_all_cypher_filters_tenant():
    for qtype, cypher in gq.QUERIES.items():
        assert 'tenant_id: $tid' in cypher, qtype
