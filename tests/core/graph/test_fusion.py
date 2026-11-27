"""融合单测：RRF 去重排序、rerank 降级、图路超时退化。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import fusion as fu
from core.rag_retriever.retriever import RetrievedChunk


def _run(coro):
    return asyncio.run(coro)


def _chunk(name, text, score=0.0):
    return RetrievedChunk(chunk_id=1, content=text, doc_name=name, score=score)


def test_rrf_dedup_and_order():
    a = _chunk('faq', '同内容')
    b = _chunk('doc', '同内容')   # doc_name 不同 → 不去重
    c = _chunk('faq', '同内容')   # 与 a 完全同键 → 去重
    out = fu.rrf_fuse([[a, b], [c]], k=60)
    assert len(out) == 2
    # 出现在两路的 a/c 得分高于只在第一路的 b
    assert out[0].score > out[1].score


def test_rrf_weights_affect_order():
    g = _chunk('graph:causal_chain', '证据')
    v = _chunk('doc', '向量')
    out = fu.rrf_fuse([[g], [v]], k=60, weights=[2.0, 1.0])
    assert out[0].doc_name == 'graph:causal_chain'


def test_rerank_no_model_falls_back():
    cands = [_chunk('a', '1'), _chunk('b', '2'), _chunk('c', '3')]
    out = _run(fu.rerank_and_refill('q', cands, None, top_k=2))
    assert [c.content for c in out] == ['1', '2']


def test_rerank_uses_model_and_refills_chain():
    a = RetrievedChunk(0, 'a', 'graph:x', meta={'chain_id': 'c1'})
    b = RetrievedChunk(0, 'b', 'graph:x', meta={'chain_id': 'c1'})
    c = _chunk('doc', 'other')
    model = mock.MagicMock()
    model.reranker = object()
    model.rerank = mock.MagicMock(return_value=[mock.MagicMock(index=0, score=0.9)])
    out = _run(fu.rerank_and_refill('q', [a, b, c], model, top_k=1))
    # rerank 选 a，链补齐把同链 b 也带回
    assert a in out and b in out


def test_graph_fused_retrieve_timeout_returns_none():
    async def slow(qtype, tid, **kw):
        await asyncio.sleep(1)
        return []
    with mock.patch.object(fu.config, 'GRAPH_QUERY_TIMEOUT', 0.01):
        out = _run(fu.graph_fused_retrieve(
            'q', 7, 'rootcause', {}, [], None, slow, lambda qt, rows: []))
    assert out is None


def test_graph_fused_retrieve_empty_returns_none():
    async def empty(qtype, tid, **kw):
        return []
    out = _run(fu.graph_fused_retrieve(
        'q', 7, 'rootcause', {}, [], None, empty, lambda qt, rows: []))
    assert out is None


def test_graph_fused_retrieve_success():
    async def search(qtype, tid, **kw):
        return [{'x': 1}]
    def render(qtype, rows):
        c = _chunk('graph:causal_chain', '证据')
        c.meta = {'origin': 'graph', 'chain_id': 'c1'}
        return [c]
    out = _run(fu.graph_fused_retrieve(
        'q', 7, 'rootcause', {}, [_chunk('doc', '向量')], None, search, render))
    assert out and len(out) == 2
    assert any(c.meta.get('origin') == 'graph' for c in out)
