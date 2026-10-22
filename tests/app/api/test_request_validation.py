"""请求体必填项非空校验单测（NonBlank：拒绝空串 / 纯空白，保留原值不 trim）。"""
import pytest
from pydantic import ValidationError

from app.api.schemas.request import ChatRequest, CreateRequestIn, FaqRequest, StopRequest, TurnRequest


@pytest.mark.parametrize("model,payload", [
    (ChatRequest, {"query": ""}),
    (ChatRequest, {"query": "   "}),
    (ChatRequest, {"query": "\t\n"}),
    (StopRequest, {"session_id": ""}),
    (StopRequest, {"session_id": "  "}),
    (TurnRequest, {"role": "user", "content": ""}),
    (TurnRequest, {"role": "user", "content": "   "}),
    (FaqRequest, {"question": "", "answer": "a"}),
    (FaqRequest, {"question": "q", "answer": "  "}),
])
def test_required_str_rejects_blank(model, payload) -> None:
    with pytest.raises(ValidationError):
        model(**payload)


def test_required_missing_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatRequest()  # query 缺失


def test_required_str_accepts_non_blank_and_preserves_value() -> None:
    # 非空白通过，且保留原值（不 trim 首尾空白）
    assert ChatRequest(query="  你好  ").query == "  你好  "
    assert StopRequest(session_id="s1").session_id == "s1"
    assert FaqRequest(question="q", answer="a").answer == "a"


_FULL_CREATE = dict(
    namespace="ns", group_id="g", data_id="d.json", domain="prompt",
    op_type=1, content_new="{}", content_format="json", title="t", reason="r",
)


def test_create_request_all_required() -> None:
    # 全部必填：空体验证失败；缺任一部分字段也失败
    with pytest.raises(ValidationError):
        CreateRequestIn()
    partial = {k: v for k, v in _FULL_CREATE.items() if k != "reason"}
    with pytest.raises(ValidationError):
        CreateRequestIn(**partial)


def test_create_request_full_valid() -> None:
    m = CreateRequestIn(**_FULL_CREATE)
    assert m.data_id == "d.json" and m.namespace == "ns"


def test_create_request_delete_allows_null_content() -> None:
    # 删除操作无内容：content_new 必填但可空（传 null）
    m = CreateRequestIn(**{**_FULL_CREATE, "op_type": 3, "content_new": None})
    assert m.content_new is None and m.op_type == 3


def test_create_request_rejects_blank_required() -> None:
    with pytest.raises(ValidationError):
        CreateRequestIn(**{**_FULL_CREATE, "namespace": "  "})
    with pytest.raises(ValidationError):
        CreateRequestIn(**{**_FULL_CREATE, "reason": ""})
