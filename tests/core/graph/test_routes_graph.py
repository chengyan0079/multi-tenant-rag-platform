"""图调试接口单测：store 可用性、查询转发、统计聚合。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.api import dashboard as dash
from app.api.dashboard import routes_graph as rg
from common.response import BizException, success as ok


def _run(coro):
    return asyncio.run(coro)


def test_store_unavailable_raises():
    with mock.patch.object(rg.app_state, 'neo4j_store', None):
        try:
            rg._store()
        except BizException as e:
            assert e.code == 50300
        else:
            raise AssertionError('应抛图库不可用')


def test_query_graph_forwards_to_search_and_render():
    store = mock.MagicMock()
    with mock.patch.object(rg.app_state, 'neo4j_store', store), \
         mock.patch.object(rg, 'graph_search', new=mock.AsyncMock(return_value=[{'x': 1}])) as search, \
         mock.patch.object(rg, 'render_evidence', return_value=[]) as render:
        out = _run(rg.query_graph('rootcause', 7))
    search.assert_awaited_once()
    render.assert_called_once_with('rootcause', [{'x': 1}])
    assert out.code == 0


def test_query_graph_rejects_unknown_qtype():
    try:
        _run(rg.query_graph('bad', 7))
    except BizException as e:
        assert e.code == 40000
    else:
        raise AssertionError('应拒绝未知 qtype')


def test_graph_stats_returns_nodes_and_edges():
    store = mock.MagicMock()
    store.query = mock.AsyncMock(side_effect=[[{'label': 'Fault', 'count': 1}], [{'type': 'MODIFIES', 'count': 2}]])
    with mock.patch.object(rg.app_state, 'neo4j_store', store):
        out = _run(rg.graph_stats())
    assert out.data['nodes'][0]['label'] == 'Fault'
    assert out.data['edges'][0]['count'] == 2
