"""app.api.schemas + ApiResponse 泛型信封单测：序列化形状、泛型 response_model 装配。

只依赖 pydantic / fastapi / common.response，不触碰 ORM 与重依赖。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import schemas as S
from common.response import ApiResponse, success


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/qa", response_model=ApiResponse[S.QAResult])
    def qa():
        return success(S.QAResult(
            answer="a", source="rag", intent="通用知识", session_id="s1",
            chunks=[S.QAChunk(content="c", doc_name="d", page=1, score=0.5)],
        ))

    @app.get("/sessions/{sid}", response_model=ApiResponse[S.SessionDetail])
    def detail(sid: str):
        return success(S.SessionDetail.model_validate({
            "session_id": sid, "title": "t",
            "content": [{"role": "user", "content": "hi"}],
            "memory": {"summary": "", "facts": []},
        }))

    @app.get("/ok", response_model=ApiResponse[S.OkData])
    def ok():
        return success(S.OkData())

    return TestClient(app)


def test_generic_response_model_serialization() -> None:
    client = _client()

    r = client.post("/qa")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["source"] == "rag"
    assert body["data"]["chunks"][0]["content"] == "c"


def test_session_detail_passthrough_content() -> None:
    client = _client()
    r = client.get("/sessions/abc")
    assert r.status_code == 200
    # content 为透传的对话原文列表，保持 {role, content} 结构
    assert r.json()["data"]["content"][0]["content"] == "hi"


def test_ok_data_shape() -> None:
    client = _client()
    assert client.get("/ok").json()["data"] == {"ok": True}


def test_session_item_from_view_dict() -> None:
    item = S.SessionItem.model_validate({
        "session_id": "s", "title": "", "preview": "", "memory_summary": "",
        "turn_count": 0, "created_at": None, "updated_at": None,
    })
    assert item.session_id == "s" and item.turn_count == 0
