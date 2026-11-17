"""图路路由单测：关键词命中、前置条件、查询参数兜底。"""
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import router as gr


def test_pick_qtype_by_keywords():
    assert gr.pick_qtype('为什么会这样') == 'rootcause'
    assert gr.pick_qtype('影响哪些服务') == 'impact'
    assert gr.pick_qtype('上次怎么处理的') == 'runbook'
    assert gr.pick_qtype('今天天气') is None


def test_should_use_graph_respects_switch():
    with mock.patch.object(gr.config, 'GRAPH_ENABLED', False):
        assert gr.should_use_graph('为什么慢', '专业咨询', False) is None
    with mock.patch.object(gr.config, 'GRAPH_ENABLED', True):
        assert gr.should_use_graph('为什么慢', '专业咨询', False) == 'rootcause'
        assert gr.should_use_graph('为什么慢', '通用知识', False) is None
        assert gr.should_use_graph('为什么慢', '专业咨询', True) is None


def test_build_query_params_defaults():
    p = gr.build_query_params('impact', '影响面')
    assert p['db_name'] == gr.config.POSTGRES_DATABASE
    p2 = gr.build_query_params('runbook', 'INC-20241117-001 怎么处理')
    assert p2['fault_id'] == 'INC-20241117-001'
    assert gr.build_query_params('runbook', '怎么处理') == {}


def test_rootcause_priority_when_multiple_keywords():
    # 规则顺序：rootcause 在前，命中即返回
    assert gr.pick_qtype('为什么影响这么大') == 'rootcause'
