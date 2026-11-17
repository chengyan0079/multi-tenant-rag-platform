"""图证据渲染单测：候选标记、逐条成候选、链补齐。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import render as gr


def test_render_rootcause_produces_three_candidates():
    rows = [{
        'change': 'CHG-21', 'config_key': 'POOL_MAX', 'before': 16, 'after': 2,
        'pool_size': 2, 'pool_waiting': 37,
        'instance_id': '10.0.0.1:8800', 'status_time': 'T+210', 'lag_s': 210,
    }]
    out = gr.render_evidence('rootcause', rows)
    assert len(out) == 1          # 一行一条候选（含三行文本），不是拆成三条
    c = out[0]
    assert c.chunk_id == 0 and c.doc_name == 'graph:causal_chain'
    assert c.meta['origin'] == 'graph'
    assert '[chg]' in c.content and '[reg]' in c.content


def test_render_impact_and_runbook():
    impact = gr.render_evidence('impact', [{
        'instance_id': 'i1', 'status': 'removed', 'health': False,
        'status_time': 'T', 'faults': ['事故A'],
    }])
    assert 'removed' in impact[0].content and '事故A' in impact[0].content

    rb = gr.render_evidence('runbook', [{
        'fault_id': 'INC-1', 'title': '连接池', 'fault_conf': 0.83,
        'runbook_title': '回滚', 'runbook_steps': ['回滚', '重启'], 'rb_conf': 0.9,
    }])
    assert '[rb]' in rb[0].content and '回滚；重启' in rb[0].content


def test_render_unknown_type_empty():
    assert gr.render_evidence('other', [{'x': 1}]) == []


def test_refill_chain_adds_siblings():
    cands = gr.render_evidence('rootcause', [
        {'change': 'a', 'instance_id': 'i1'}, {'change': 'b', 'instance_id': 'i2'},
    ])
    picked = [cands[0]]
    merged = gr.refill_chain(cands, picked)
    assert len(merged) == 2
    assert merged[1] is cands[1]


def test_refill_chain_no_chain_id_noop():
    from core.rag_retriever.retriever import RetrievedChunk
    a = RetrievedChunk(0, 'a', '')
    b = RetrievedChunk(0, 'b', '')
    assert gr.refill_chain([a, b], [a]) == [a]
