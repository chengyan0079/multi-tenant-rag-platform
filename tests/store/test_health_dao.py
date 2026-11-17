"""健康检查 DAO 单测：图路状态取自内存，不发网络请求。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from store import health_dao as hd


def _run(coro):
    return asyncio.run(coro)


def test_graph_ok_false_when_store_missing():
    with mock.patch.object(hd.app_state, 'neo4j_store', None):
        assert hd._graph_ok() is False


def test_graph_ok_false_when_driver_missing():
    store = mock.MagicMock()
    store.driver = None
    with mock.patch.object(hd.app_state, 'neo4j_store', store):
        assert hd._graph_ok() is False


def test_graph_ok_true_when_connected():
    store = mock.MagicMock()
    store.driver = object()
    with mock.patch.object(hd.app_state, 'neo4j_store', store):
        assert hd._graph_ok() is True


def test_check_includes_graph_without_network():
    with mock.patch.object(hd, '_redis_ping', new=mock.AsyncMock(return_value=True)), \
         mock.patch.object(hd, '_db_ping', new=mock.AsyncMock(return_value=True)), \
         mock.patch.object(hd.app_state, 'neo4j_store', None):
        result = _run(hd.HealthDao().check())
    assert result['graph'] is False
    assert set(result) == {'redis', 'postgres', 'milvus', 'model',
                           'mcp', 'nacos', 'config_center', 'graph'}
