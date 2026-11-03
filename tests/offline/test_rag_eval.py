"""offline.rag_eval 单测：仅测数据集加载与字段校验（不依赖 ragas 运行时）。"""
import json

from common.config import config


def _import_mod():
    try:
        from offline import rag_eval
        return rag_eval
    except ImportError:
        import pytest
        pytest.skip("rag_eval 依赖不可用，跳过")


def test_load_dataset_array(tmp_path) -> None:
    rag_eval = _import_mod()
    data = [
        {"user_input": "q1", "response": "a1", "retrieved_contexts": ["c1"], "reference": "r1"},
        {"user_input": "q2", "response": "a2", "retrieved_contexts": ["c2"], "reference": "r2"},
    ]
    p = tmp_path / "eval.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = rag_eval.load_dataset(str(p))
    assert len(loaded) == 2
    assert loaded[0]["user_input"] == "q1"


def test_load_dataset_wrapped(tmp_path) -> None:
    rag_eval = _import_mod()
    data = {"samples": [{"user_input": "q", "response": "a", "retrieved_contexts": ["c"], "reference": "r"}]}
    p = tmp_path / "eval.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert len(rag_eval.load_dataset(str(p))) == 1


def test_load_dataset_missing_field_raises(tmp_path) -> None:
    rag_eval = _import_mod()
    data = [{"user_input": "q", "response": "a", "retrieved_contexts": ["c"]}]  # 缺 reference
    p = tmp_path / "eval.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    try:
        rag_eval.load_dataset(str(p))
        assert False, "应因缺字段抛错"
    except ValueError:
        pass


def test_config_has_ragas_fields() -> None:
    # 评测配置项存在，保证 offline/rag_eval 能取到端点
    assert hasattr(config, "RAGAS_LLM_BASE_URL")
    assert hasattr(config, "RAGAS_EMBED_MODEL")


def test_resolve_contexts_prefers_fulltext_then_lookup() -> None:
    rag_eval = _import_mod()
    refs = [
        {"chunk_id": 1, "text": "全文片段A"},          # 自带全文，直接采用
        {"chunk_id": 2},                                  # 仅引用，回查命中
        {"chunk_id": 3},                                  # 仅引用，回查未命中 -> 丢弃
        {"chunk_id": 4, "text": ""},                     # 全文为空，回查命中
    ]
    content_map = {2: "回查片段B", 4: "回查片段D"}
    contexts = rag_eval._resolve_contexts(refs, content_map)
    assert contexts == ["全文片段A", "回查片段B", "回查片段D"]


def test_resolve_contexts_empty() -> None:
    rag_eval = _import_mod()
    assert rag_eval._resolve_contexts([], {}) == []
    assert rag_eval._resolve_contexts([{"chunk_id": 9}], {}) == []  # 未命中且无全文
