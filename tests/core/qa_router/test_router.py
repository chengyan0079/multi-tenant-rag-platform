"""
test_router - 问答路由单测（pytest + mock）

mock 掉 classify_query、Redis 缓存、MySQL FAQ 查询，以及注入假的 retriever/generator/
strategy_selector/augmenter，验证"意图分流 + 检索增强"主流程。
用 asyncio.run 包裹异步逻辑，避免依赖 pytest-asyncio 插件。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

from common.config import config

# MySQL 层依赖 sqlalchemy[asyncio]/greenlet，测试环境可能缺失；此处以 mock 注入，
# 使 router 模块可导入（MySQL 交互在单测中本就被 mock 掉，不实际触达）。
sys.modules.setdefault("store.mysql_store", mock.MagicMock())

import core.qa_router.router as m
from core.qa_router.compression import CompressResult
from core.rag_retriever.strategy_selector import RetrievalStrategy


def _passthrough_compress(q, c, s):
    """压缩占位：原样返回（模拟未触发压缩）。"""
    return CompressResult(chunks=c, method='none', kept_indices=list(range(len(c))))


def _make_router():
    retr = mock.AsyncMock()
    retr.retrieve.return_value = []  # FAQ 未命中
    gen = mock.AsyncMock()
    gen.generate.return_value = "答案"
    strat = mock.AsyncMock()
    strat.select_strategy.return_value = RetrievalStrategy.DIRECT
    aug = mock.AsyncMock()
    aug.augment_and_retrieve.return_value = [
        mock.MagicMock(chunk_id=1, content="c", doc_name="d", page=1, rerank_score=0.9, meta={})
    ]
    router = m.QARouter(retriever=retr, generator=gen, strategy_selector=strat, augmenter=aug)
    return router, retr, gen, strat, aug


def test_professional_uses_augmenter():
    async def _run():
        with mock.patch.object(m, "classify_query", return_value="专业咨询"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer") as set_ans:
            router, retr, gen, strat, aug = _make_router()
            with mock.patch.object(router, "_compress_context",
                                   new=mock.AsyncMock(side_effect=_passthrough_compress)) as cmp, \
                 mock.patch.object(router, "_persist_session", new=mock.AsyncMock()) as ps, \
                 mock.patch.object(router, "_audit_llm_result", new=mock.AsyncMock()) as au:
                res = await router.answer("问题", tenant_id=1)
        strat.select_strategy.assert_awaited_once_with("问题")
        aug.augment_and_retrieve.assert_awaited_once()
        gen.generate.assert_awaited_once()
        cmp.assert_awaited_once()   # 上下文压缩占位已接入
        ps.assert_awaited_once()    # 会话记录持久化占位已接入
        au.assert_awaited_once()    # LLM 结果审计占位已接入
        set_ans.assert_awaited_once()
        assert res["source"] == "rag"
        assert res["intent"] == "专业咨询"
    asyncio.run(_run())


def test_general_knowledge_answers_directly_without_retrieval():
    async def _run():
        with mock.patch.object(m, "classify_query", return_value="通用知识"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer"):
            router, retr, gen, strat, aug = _make_router()
            with mock.patch.object(router, "_compress_context",
                                   new=mock.AsyncMock(side_effect=_passthrough_compress)), \
                 mock.patch.object(router, "_persist_session", new=mock.AsyncMock()), \
                 mock.patch.object(router, "_audit_llm_result", new=mock.AsyncMock()):
                res = await router.answer("问题", tenant_id=1)
        strat.select_strategy.assert_not_called()     # 通用知识不选检索策略
        aug.augment_and_retrieve.assert_not_called()  # 通用知识不检索
        gen.generate.assert_awaited_once()            # 空上下文交由大模型用自身知识直答
        assert res["source"] == "rag" and res["intent"] == "通用知识"
        assert res["chunks"] == []
    asyncio.run(_run())


def test_professional_no_hits_returns_not_found():
    async def _run():
        with mock.patch.object(m, "classify_query", return_value="专业咨询"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer"):
            router, retr, gen, strat, aug = _make_router()
            aug.augment_and_retrieve.return_value = []   # 专业咨询检索无命中
            res = await router.answer("问题", tenant_id=1)
        gen.generate.assert_not_called()                 # 无命中不生成
        assert res["source"] == "empty" and res["answer"] == "抱歉，未找到答案！"
    asyncio.run(_run())


def test_cache_hit_shortcuts_pipeline():
    async def _run():
        with mock.patch.object(m, "classify_query") as ci, \
             mock.patch.object(m, "get_answer", return_value="缓存答案"):
            router, retr, gen, strat, aug = _make_router()
            res = await router.answer("问题", tenant_id=1)
        ci.assert_not_called()      # 缓存命中后不再做意图识别
        aug.augment_and_retrieve.assert_not_called()
        gen.generate.assert_not_called()
        assert res["source"] == "cache"
        assert res["intent"] == "通用知识"
    asyncio.run(_run())


class _StreamGen:
    """提供 astream 异步生成器的假生成器（RAG 流式路径）。"""

    async def astream(self, **kw):
        for t in ["你", "好", "世界"]:
            yield t


def test_answer_stream_rag_yields_meta_tokens_done():
    async def _run():
        retr = mock.AsyncMock()
        retr.retrieve.return_value = []
        strat = mock.AsyncMock()
        strat.select_strategy.return_value = RetrievalStrategy.DIRECT
        aug = mock.AsyncMock()
        aug.augment_and_retrieve.return_value = [
            mock.MagicMock(chunk_id=1, content="c", doc_name="d", page=1, rerank_score=0.9, meta={})
        ]
        router = m.QARouter(retriever=retr, generator=_StreamGen(), strategy_selector=strat, augmenter=aug)
        events = []
        with mock.patch.object(m, "classify_query", return_value="专业咨询"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer") as set_ans, \
             mock.patch.object(router, "_compress_context",
                               new=mock.AsyncMock(side_effect=_passthrough_compress)), \
             mock.patch.object(router, "_persist_session", new=mock.AsyncMock()), \
             mock.patch.object(router, "_audit_llm_result", new=mock.AsyncMock()):
            async for ev, data in router.answer_stream("问题", tenant_id=1):
                events.append((ev, data))
        kinds = [e for e, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        assert "".join(d for e, d in events if e == "token") == "你好世界"
        set_ans.assert_awaited_once()   # 流式结束后缓存完整答案
    asyncio.run(_run())


def test_cache_and_faq_branches_also_persist():
    """审计完整性：缓存/FAQ 命中同样落明细，source 要能区分。"""
    async def _run():
        for cached, source in [("缓存答案", "cache"), (None, "faq")]:
            with mock.patch.object(m, "classify_query", return_value="专业咨询"), \
                 mock.patch.object(m, "get_answer", return_value=cached), \
                 mock.patch.object(m, "get_faq_answer", return_value=("FAQ 答案", 1)), \
                 mock.patch.object(m, "set_answer"):
                router, *_ = _make_router()
                with mock.patch.object(router, "_persist_session", new=mock.AsyncMock()) as ps:
                    res = await router.answer("问题", tenant_id=1, session_id="s1", user_id="u1")
            ps.assert_awaited_once()
            assert ps.await_args.kwargs["meta"]["source"] == source
            assert res["source"] == source
    asyncio.run(_run())


def test_persist_meta_carries_compress_and_source():
    """RAG 分支的落库要带上压缩方式与丢弃数，供可观测与评测取数。"""
    async def _run():
        with mock.patch.object(m, "classify_query", return_value="专业咨询"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer"):
            router, *_ = _make_router()
            with mock.patch.object(router, "_compress_context",
                                   new=mock.AsyncMock(side_effect=_passthrough_compress)), \
                 mock.patch.object(router, "_persist_session", new=mock.AsyncMock()) as ps, \
                 mock.patch.object(router, "_audit_llm_result", new=mock.AsyncMock()):
                await router.answer("问题", tenant_id=1, session_id="s1", user_id="u1")
        meta = ps.await_args.kwargs["meta"]
        assert meta["source"] == "rag" and meta["tenant_id"] == 1
        assert "turn_id" in meta and meta["latency_ms"] >= 0
    asyncio.run(_run())


def test_persist_failure_does_not_break_answer():
    """落库失败不得影响主链路：异常被吞掉，回答照常返回。"""
    async def _run():
        with mock.patch.object(m, "classify_query", return_value="通用知识"), \
             mock.patch.object(m, "get_answer", return_value=None), \
             mock.patch.object(m, "set_answer"):
            router, *_ = _make_router()
            with mock.patch.object(router, "_compress_context",
                                   new=mock.AsyncMock(side_effect=_passthrough_compress)), \
                 mock.patch.object(router, "_audit_llm_result", new=mock.AsyncMock()), \
                 mock.patch.object(config, "QA_PERSIST_ASYNC", False), \
                 mock.patch.object(router.session_store, "persist_turn_rows",
                                   new=mock.AsyncMock(side_effect=RuntimeError("MySQL 挂了"))):
                res = await router.answer("问题", tenant_id=1, session_id="s1", user_id="u1")
        assert res["source"] == "rag" and res["answer"] == "答案"
    asyncio.run(_run())


def test_answer_stream_cache_hit_emits_answer_and_done():
    async def _run():
        router, *_ = _make_router()
        events = []
        with mock.patch.object(m, "classify_query") as ci, \
             mock.patch.object(m, "get_answer", return_value="缓存答案"):
            async for ev, data in router.answer_stream("问题", tenant_id=1):
                events.append((ev, data))
        ci.assert_not_called()          # 缓存命中不再意图识别
        kinds = [e for e, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        assert [d for e, d in events if e == "answer"] == ["缓存答案"]
    asyncio.run(_run())
