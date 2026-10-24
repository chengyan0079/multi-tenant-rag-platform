"""
test_compression - 检索片段压缩单测（pytest + mock）

覆盖设计方案 docs/上下文压缩设计方案.md §5 的各档策略：
none / truncate（L0 按大小截取 + 按句截短）/ select（L1 相关性精选）/ llm_summary（L2 摘要与失败回退）。
用 asyncio.run 包裹异步逻辑，避免依赖 pytest-asyncio 插件。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

sys.modules.setdefault("store.postgres_store", mock.MagicMock())

from common.config import config
from core.qa_router import compression
from core.rag_retriever.retriever import RetrievedChunk


def _chunk(idx: int, content: str, score: float = 0.5) -> RetrievedChunk:
    """构造一个检索片段：溯源字段与分数都要有，压缩后需能追溯来源。"""
    return RetrievedChunk(chunk_id=idx, content=content, doc_name=f'doc{idx}.pdf',
                          page=idx, rerank_score=score)


def _run(coro):
    return asyncio.run(coro)


def test_strategy_none_returns_original():
    chunks = [_chunk(0, '内容一' * 100, 0.9)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'none'):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'none'
    assert res.chunks is chunks
    assert res.dropped == 0


def test_empty_chunks_is_noop():
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'truncate'):
        res = _run(compression.compress_context('问题', []))
    assert res.method == 'none' and res.chunks == [] and res.chars_before == 0


def test_truncate_by_total_chars_keeps_top_scored():
    """总量超出软上限时，按 rerank 分数降序保留，后面的整条丢掉。"""
    chunks = [_chunk(0, '甲' * 1000, 0.9), _chunk(1, '乙' * 1000, 0.8), _chunk(2, '丙' * 1000, 0.1)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'truncate'), \
         mock.patch.object(config, 'COMPRESS_MAX_TOTAL_CHARS', 1500), \
         mock.patch.object(config, 'COMPRESS_MAX_CHUNK_CHARS', 0):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'truncate'
    assert [c.chunk_id for c in res.chunks] == [0]     # 只留分数最高的那条（第二条会超限）
    assert res.dropped == 2
    assert res.chars_before == 3000 and res.chars_after == 1000


def test_long_chunk_truncated_at_sentence_boundary():
    """单片段超限按句截断，不能在句子中间切断。"""
    long_text = '第一句话。' * 10
    chunks = [_chunk(0, long_text, 0.9)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'truncate'), \
         mock.patch.object(config, 'COMPRESS_MAX_CHUNK_CHARS', 22):
        res = _run(compression.compress_context('问题', chunks))
    content = res.chunks[0].content
    assert content.endswith(compression._TRUNCATE_SUFFIX)
    assert '第一句话。' in content
    assert content.count('第一句话。') == 4        # 4×5=20 字符，再加一句就超 22
    assert res.chunks[0].doc_name == 'doc0.pdf'   # 溯源字段不因截断丢失


def test_select_drops_low_score_chunks():
    chunks = [_chunk(0, '高分内容', 0.9), _chunk(1, '低分内容', 0.1)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'select'), \
         mock.patch.object(config, 'COMPRESS_MAX_TOTAL_CHARS', 10000), \
         mock.patch.object(config, 'COMPRESS_SIM_THRESHOLD', 0.3):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'select'
    assert [c.chunk_id for c in res.chunks] == [0]
    assert res.dropped == 1


def test_select_keeps_all_when_everything_below_threshold():
    """全部低于阈值时保留原集合：宁可超一点也不丢空上下文。"""
    chunks = [_chunk(0, '甲', 0.1), _chunk(1, '乙', 0.2)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'select'), \
         mock.patch.object(config, 'COMPRESS_MAX_TOTAL_CHARS', 10000), \
         mock.patch.object(config, 'COMPRESS_SIM_THRESHOLD', 0.3):
        res = _run(compression.compress_context('问题', chunks))
    assert len(res.chunks) == 2 and res.dropped == 0


def test_llm_summary_merges_sources():
    """超硬上限且开启 L2：走摘要，输出单个合成片段并汇总来源。"""
    chunks = [_chunk(0, '甲' * 100, 0.9), _chunk(1, '乙' * 100, 0.8)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'summary'), \
         mock.patch.object(config, 'COMPRESS_LLM_SUMMARY_ENABLED', True), \
         mock.patch.object(config, 'COMPRESS_HARD_TOTAL_CHARS', 50), \
         mock.patch('core.rag_model.llm_client.acomplete', new=mock.AsyncMock(return_value='压缩后的摘要')):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'llm_summary'
    assert len(res.chunks) == 1
    assert res.chunks[0].content == '压缩后的摘要'
    assert res.chunks[0].doc_name == 'doc0.pdf、doc1.pdf'   # 溯源汇总，不能凭空生成无引用内容
    assert res.chunks[0].meta['source_indices'] == [0, 1]


def test_llm_summary_failure_falls_back_to_truncate():
    """L2 失败不得影响主链路：回退到强制截取。"""
    chunks = [_chunk(0, '甲' * 100, 0.9), _chunk(1, '乙' * 100, 0.8)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'summary'), \
         mock.patch.object(config, 'COMPRESS_LLM_SUMMARY_ENABLED', True), \
         mock.patch.object(config, 'COMPRESS_HARD_TOTAL_CHARS', 50), \
         mock.patch('core.rag_model.llm_client.acomplete', new=mock.AsyncMock(side_effect=RuntimeError('LLM 挂了'))):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'truncate'
    assert len(res.chunks) == 1                # 强制截到硬上限以内
    assert res.chunks[0].chunk_id == 0


def test_no_change_reported_as_none():
    """未超限时应报 none：否则每个请求都记成 truncate，压缩触发率指标会失真。"""
    chunks = [_chunk(0, '短内容一', 0.9), _chunk(1, '短内容二', 0.8)]
    with mock.patch.object(config, 'COMPRESS_STRATEGY', 'truncate'), \
         mock.patch.object(config, 'COMPRESS_MAX_TOTAL_CHARS', 8000), \
         mock.patch.object(config, 'COMPRESS_MAX_CHUNK_CHARS', 1500):
        res = _run(compression.compress_context('问题', chunks))
    assert res.method == 'none'
    assert res.dropped == 0 and res.chars_before == res.chars_after


def test_result_to_dict_exposes_metrics():
    """响应与日志要能拿到压缩方式与丢弃数（设计方案 §7 可观测性）。"""
    res = compression.CompressResult(chunks=[], method='truncate',
                                     chars_before=100, chars_after=50, kept_indices=[0], dropped=2)
    assert res.to_dict() == {'method': 'truncate', 'chars_before': 100, 'chars_after': 50,
                             'kept_indices': [0], 'dropped': 2}
