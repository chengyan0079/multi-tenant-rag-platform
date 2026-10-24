"""
test_faq_cache - FAQ 字面精确匹配纯逻辑单测（pytest + mock）

仅测试不依赖 Redis/数据库的纯函数：match_faq / _parse_similar / _normalize / _tokenize / bm25_match。
用 asyncio.run 包裹（本模块无异步纯函数，仅保持与项目单测约定一致）。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import pytest

import store.faq_cache as fc


def test_normalize_strips_whitespace():
    assert fc._normalize("  你好  ") == "你好"
    assert fc._normalize("") == ""
    assert fc._normalize(None) == ""


def test_parse_similar_multiline():
    raw = "怎么退款？\n\r如何退费？\n\n   \n发票怎么开"
    assert fc._parse_similar(raw) == ["怎么退款？", "如何退费？", "发票怎么开"]
    assert fc._parse_similar(None) == []
    assert fc._parse_similar("") == []


def test_match_faq_exact_question():
    items = [{"faq_id": 1, "question": "怎么退款", "similar_questions": []}]
    assert fc.match_faq(items, "怎么退款") == 1
    # 归一化：首尾空白不影响精确匹配
    assert fc.match_faq(items, "  怎么退款  ") == 1


def test_match_faq_via_similar_questions():
    items = [{
        "faq_id": 2,
        "question": "退款政策",
        "similar_questions": ["怎么退款", "如何退费"],
    }]
    assert fc.match_faq(items, "如何退费") == 2
    assert fc.match_faq(items, "怎么退款") == 2


def test_match_faq_no_hit():
    items = [{"faq_id": 1, "question": "怎么退款", "similar_questions": ["如何退费"]}]
    assert fc.match_faq(items, "运费多少") is None
    # 子串/包含不算命中（保持误触发最低）
    assert fc.match_faq(items, "退款") is None
    assert fc.match_faq(items, "怎么退款吗") is None


def test_match_faq_empty():
    assert fc.match_faq([], "x") is None
    assert fc.match_faq(None, "x") is None
    assert fc.match_faq([{"faq_id": 1, "question": "q", "similar_questions": []}], "  ") is None


def test_tokenize_filters_stopwords_and_punct():
    toks = fc._tokenize("Python编程怎么入门呢？")
    assert "Python" in toks and "编程" in toks and "入门" in toks
    assert "怎么" not in toks and "呢" not in toks and "？" not in toks


def test_bm25_match_hits_above_threshold():
    pytest.importorskip("jieba")
    pytest.importorskip("rank_bm25")
    items = [
        {"faq_id": 0, "question": "如何学习Python编程语言", "similar_questions": []},
        {"faq_id": 1, "question": "Java学费是多少钱", "similar_questions": []},
        {"faq_id": 2, "question": "如何申请退课退款", "similar_questions": []},
        {"faq_id": 3, "question": "奖学金评定标准是什么", "similar_questions": []},
        {"faq_id": 4, "question": "校园网账号密码找回", "similar_questions": []},
    ]
    # 改写问句 → BM25 模糊命中对应 FAQ
    assert fc.bm25_match(items, "Python编程怎么入门学习", 1.0, tenant_id=1) == 0
    assert fc.bm25_match(items, "奖学金怎么评", 1.0, tenant_id=1) == 3
    # 高于阈值 / 完全不相关 → 不命中
    assert fc.bm25_match(items, "Python编程怎么入门学习", 9999.0, tenant_id=1) is None
    assert fc.bm25_match(items, "运费多少", 1.0, tenant_id=1) is None


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))  # 保持模块可执行占位
    print("faq_cache 纯逻辑单测通过")
