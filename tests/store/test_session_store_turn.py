"""
test_session_store_turn - 问答明细（rag_turn）持久化单测（pytest + mock）

覆盖 docs/会话记录持久化设计方案.md §5 的数据结构与写入行为：
turn 元素结构、片段只存引用、Redis 追加与裁剪、MySQL 落库幂等、删会话联动。
"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

sys.modules.setdefault("store.mysql_store", mock.MagicMock())

from common.config import config
from store import session_store as ss


class _Chunk:
    """假片段：既支持属性访问（对齐 RetrievedChunk），用于验证引用提取。"""

    def __init__(self, cid, content, doc_name, page, score):
        self.chunk_id = cid
        self.content = content
        self.doc_name = doc_name
        self.page = page
        self.rerank_score = score


def _run(coro):
    return asyncio.run(coro)


def test_chunk_refs_only_keeps_reference_by_default():
    refs = ss._chunk_refs([_Chunk(1, '正文内容', '手册.pdf', 12, 0.91)])
    assert refs == [{'chunk_id': 1, 'doc_name': '手册.pdf', 'page': 12, 'score': 0.91}]
    assert 'text' not in refs[0]      # 默认不存全文：避免同一份文本多处复制后版本不一致


def test_chunk_refs_includes_text_when_enabled():
    with mock.patch.object(config, 'QA_PERSIST_CHUNK_FULLTEXT', True):
        refs = ss._chunk_refs([_Chunk(1, '正文内容', '手册.pdf', 12, 0.91)])
    assert refs[0]['text'] == '正文内容'


def test_chunk_refs_accepts_dict_chunks():
    refs = ss._chunk_refs([{'chunk_id': 2, 'doc_name': 'd', 'page': 1, 'rerank_score': 0.5}])
    assert refs[0]['chunk_id'] == 2 and refs[0]['score'] == 0.5


def test_build_turn_payload_puts_qa_detail_on_assistant_only():
    """一轮两条：user 轻量，QA 明细只挂 assistant（产出侧属性）。"""
    store = ss.SessionStore()
    user_item, asst_item, rows = store.build_turn_payload(
        turn_id='t1', user_id='u1', session_id='s1', query='问什么', answer='答什么',
        tenant_id=7, intent='专业咨询', source='rag', strategy='子查询检索',
        model='deepseek-flash', chunks=[_Chunk(1, 'x', '手册.pdf', 3, 0.8)],
        prompt_tokens=10, completion_tokens=20, latency_ms=500,
        compress_method='truncate', compress_dropped=2, eval_flag=1)

    assert user_item['role'] == 'user' and list(user_item) == ['role', 'content', 'ts', 'turn_id']
    assert asst_item['role'] == 'assistant'
    assert asst_item['source'] == 'rag' and asst_item['strategy'] == '子查询检索'
    assert asst_item['compress'] == {'method': 'truncate', 'dropped': 2}
    assert len(rows) == 2
    assert rows[0]['role'] == 'user' and rows[0]['eval_flag'] == 1
    assert rows[1]['role'] == 'assistant' and rows[1]['tenant_id'] == 7
    assert '手册.pdf' in rows[1]['chunks_json']


def test_new_turn_id_unique():
    store = ss.SessionStore()
    assert store.new_turn_id() != store.new_turn_id()


def test_append_turn_pushes_then_expires_and_trims():
    """一轮两条一次 RPUSH（保证顺序），随后续期并按上限裁剪。"""
    store = ss.SessionStore()
    redis = mock.AsyncMock()
    with mock.patch.object(ss.app_state, 'redis', redis), \
         mock.patch.object(config, 'QA_PERSIST_MAX_TURNS', 10):
        _run(store.append_turn('u1', 's1', {'role': 'user'}, {'role': 'assistant'}))
    redis.rpush.assert_awaited_once()
    assert redis.rpush.await_args.args[0] == 'u:u1:sess:s1'
    assert len(redis.rpush.await_args.args) == 3       # key + 两条元素
    redis.expire.assert_awaited_once()
    redis.ltrim.assert_awaited_once_with('u:u1:sess:s1', -10, -1)


def test_append_turn_noop_without_redis():
    store = ss.SessionStore()
    with mock.patch.object(ss.app_state, 'redis', None):
        assert _run(store.append_turn('u1', 's1', {'role': 'user'})) is None


def test_persist_turn_rows_is_idempotent_on_duplicate():
    """唯一键冲突视为成功：重试/重放不报错、不重复写。"""
    from sqlalchemy.exc import IntegrityError

    store = ss.SessionStore()
    session = mock.MagicMock()
    session.commit = mock.AsyncMock(side_effect=IntegrityError('stmt', {}, Exception('dup')))
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=session)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    with mock.patch.object(ss, 'get_session', return_value=ctx):
        _run(store.persist_turn_rows([{'turn_id': 't1', 'role': 'user'}]))
    session.add_all.assert_called_once()


def test_persist_turn_rows_swallows_other_errors():
    """落库失败只告警，不抛到主链路。"""
    store = ss.SessionStore()
    with mock.patch.object(ss, 'get_session', side_effect=RuntimeError('MySQL 挂了')):
        assert _run(store.persist_turn_rows([{'turn_id': 't1'}])) is None


def test_delete_turn_rows_executes_delete_for_session():
    store = ss.SessionStore()
    session = mock.AsyncMock()
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=session)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    with mock.patch.object(ss, 'get_session', return_value=ctx):
        _run(store.delete_turn_rows('u1', 's1'))
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
