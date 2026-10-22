"""
test_query_augmenter - 检索增强/改写单测（pytest + mock）

mock 掉 Retriever、RAGPromptsManager 的改写提示词与 acomplete（LLM 调用）。
用 asyncio.run 包裹异步逻辑，避免依赖 pytest-asyncio 插件。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

import core.rag_retriever.query_augmenter as m
from core.rag_retriever.strategy_selector import RetrievalStrategy


def _chunk(cid, content):
    return m.RetrievedChunk(chunk_id=cid, content=content)


def _fake_template():
    class T:
        def format(self, query):
            return f"rendered:{query}"
    return T()


def test_direct_calls_retriever_once():
    async def _run():
        retr = mock.AsyncMock()
        retr.retrieve.return_value = [_chunk(1, "a")]
        aug = m.QueryAugmenter(retriever=retr)
        res = await aug.augment_and_retrieve("q", RetrievalStrategy.DIRECT, tenant_id=1)
        retr.retrieve.assert_awaited_once()
        assert res[0].chunk_id == 1
    asyncio.run(_run())


def test_hyde_uses_hypothetical_doc():
    async def _run():
        retr = mock.AsyncMock()
        retr.retrieve.return_value = [_chunk(2, "b")]
        with mock.patch.object(m.RAGPromptsManager, "hyde_gen_prompt", return_value=_fake_template()), \
             mock.patch.object(m, "acomplete", return_value="假设文档内容"):
            aug = m.QueryAugmenter(retriever=retr)
            res = await aug.augment_and_retrieve("q", RetrievalStrategy.HYDE, tenant_id=1)
        assert retr.retrieve.call_args.kwargs["query"] == "假设文档内容"
        assert res[0].chunk_id == 2
    asyncio.run(_run())


def test_subquery_merges_and_dedup():
    async def _run():
        retr = mock.AsyncMock()
        retr.retrieve.side_effect = [
            [_chunk(1, "a")],
            [_chunk(1, "a"), _chunk(2, "b")],
        ]
        with mock.patch.object(m.RAGPromptsManager, "subq_gen_prompt", return_value=_fake_template()), \
             mock.patch.object(m, "acomplete", return_value="子问题1\n子问题2"):
            aug = m.QueryAugmenter(retriever=retr)
            res = await aug.augment_and_retrieve("q", RetrievalStrategy.SUB_QUERY, tenant_id=1)
        # 子查询1命中[1]、子查询2命中[1,2]，跨子查询去重后应为[1,2]
        assert [c.chunk_id for c in res] == [1, 2]
    asyncio.run(_run())


def test_augmentation_empty_falls_back_direct():
    async def _run():
        retr = mock.AsyncMock()
        retr.retrieve.side_effect = [[], [_chunk(9, "z")]]
        with mock.patch.object(m.RAGPromptsManager, "hyde_gen_prompt", return_value=_fake_template()), \
             mock.patch.object(m, "acomplete", return_value="假设文档"):
            aug = m.QueryAugmenter(retriever=retr)
            res = await aug.augment_and_retrieve("q", RetrievalStrategy.HYDE, tenant_id=1)
        assert retr.retrieve.await_count == 2
        assert res[0].chunk_id == 9
    asyncio.run(_run())


def test_merge_chunks_dedup_and_truncate():
    sets = [[_chunk(1, "a"), _chunk(2, "b")], [_chunk(2, "b"), _chunk(3, "c")]]
    out = m._merge_chunks(sets, top_k=2)
    assert [c.chunk_id for c in out] == [1, 2]
