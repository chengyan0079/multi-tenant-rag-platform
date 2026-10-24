"""routes_session 单测：验证服务端建会话、user 级隔离、缺 user_id 拒绝。

用内存假 Redis + FastAPI TestClient。PostgreSQL 注册表（rag_session）以内存实现替代，
避免依赖 greenlet / 真实 DB（store.postgres_store 仅被 mock 用于绕过其顶层 import）。
"""
import sys
from unittest import mock

# 仅用于绕过 store.postgres_store 的 import（其顶层导入 sqlalchemy.ext.asyncio 需 greenlet）；
# 真正的注册表读写由下方 monkeypatch 的内存实现替代。
sys.modules.setdefault("store.postgres_store", mock.MagicMock())
# 避免 app.api.user 包 __init__ 连带导入 routes_qa / dashboard 路由时报缺依赖
for _name in [
    "app.api.dashboard.routes_file",
    "app.api.dashboard.routes_doc",
    "app.api.dashboard.routes_faq",
    "app.api.dashboard.routes_config_read",
    "app.api.dashboard.routes_config_write",
    "app.api.dashboard.routes_retrieve",
    "app.api.dashboard.routes_health",
    "app.api.user.routes_qa",
]:
    sys.modules.setdefault(_name, mock.MagicMock())

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.user import routes_session
from app.app_state import app_state
from common.config import config
from store.session_store import SessionStore
from store.session_store import SessionView


# ---- 内存版会话注册表（替代 PostgreSQL，保证 GET 列表/详情可回看） ----
_REGISTRY: dict[tuple, dict] = {}


async def _upsert(self, uid, sid, *, title=None, preview=None,
                  memory_summary=None, turn_count=None, inc_turn=False):
    k = (uid, sid)
    row = _REGISTRY.get(k)
    if row is None:
        row = {"title": "", "preview": "", "memory_summary": "", "turn_count": 0}
        _REGISTRY[k] = row
    if title is not None:
        row["title"] = title
    if preview is not None:
        row["preview"] = preview
    if memory_summary is not None:
        row["memory_summary"] = memory_summary
    if turn_count is not None:
        row["turn_count"] = turn_count
    if inc_turn:
        row["turn_count"] += 1


async def _get(self, uid, sid):
    row = _REGISTRY.get((uid, sid))
    if row is None:
        return None
    return SessionView(session_id=sid, title=row["title"], preview=row["preview"],
                       memory_summary=row["memory_summary"], turn_count=row["turn_count"])


async def _list(self, uid):
    return [SessionView(session_id=k[1], title=v["title"], preview=v["preview"],
                        memory_summary=v["memory_summary"], turn_count=v["turn_count"])
            for k, v in _REGISTRY.items() if k[0] == uid]


async def _delete(self, uid, sid):
    _REGISTRY.pop((uid, sid), None)


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch):
    monkeypatch.setattr(SessionStore, "registry_upsert", _upsert)
    monkeypatch.setattr(SessionStore, "registry_get", _get)
    monkeypatch.setattr(SessionStore, "registry_list", _list)
    monkeypatch.setattr(SessionStore, "registry_delete", _delete)
    _REGISTRY.clear()
    yield


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[tuple, object] = {}

    async def rpush(self, key: str, value: str):
        self.data.setdefault(("list", key), []).append(value)
        return len(self.data[("list", key)])

    async def lrange(self, key: str, start: int, end: int):
        lst = self.data.get(("list", key), [])
        n = len(lst)
        s = start if start >= 0 else max(0, n + start)
        e = end if end >= 0 else n + end
        return lst[s:e + 1] if e >= 0 else lst[s:]

    async def expire(self, key: str, ttl: int):
        return 1

    async def sadd(self, key: str, member: str):
        self.data.setdefault(("set", key), set()).add(member)
        return 1

    async def smembers(self, key: str):
        return set(self.data.get(("set", key), set()))

    async def hset(self, key: str, mapping=None):
        self.data.setdefault(("hash", key), {}).update(mapping or {})
        return 1

    async def hgetall(self, key: str):
        return dict(self.data.get(("hash", key), {}))

    async def delete(self, key: str):
        for p in ("list", "set", "hash"):
            self.data.pop((p, key), None)
        return 1

    async def get(self, key: str):
        return None


@pytest.fixture
def client():
    app_state.redis = FakeRedis()
    app = FastAPI()
    app.include_router(routes_session.router)
    yield TestClient(app)
    app_state.redis = None


def test_create_session_uses_header_or_default(client, monkeypatch):
    # 未接网关：缺 X-User-Id 兜底为 DEFAULT_USER_ID（默认 1001）
    monkeypatch.setattr(config, 'IDENTITY_DEFAULT_ENABLED', True)
    monkeypatch.setattr(config, 'DEFAULT_USER_ID', '1001')
    r = client.post("/v1/sessions", headers={"X-Tenant-Id": "1"})
    assert r.status_code == 200
    sid = r.json()["data"]["session_id"]
    # 依然不带 user 头能读回 → 两次请求落到同一个默认用户
    assert client.get(f"/v1/sessions/{sid}").status_code == 200

    # 带 user -> 200 且返回服务端生成的 session_id + 标题
    r = client.post("/v1/sessions", headers={"X-Tenant-Id": "1", "X-User-Id": "u1"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert isinstance(body["session_id"], str) and body["session_id"]
    assert "title" in body


def test_create_session_rejects_when_fallback_off(client, monkeypatch):
    # 接入网关后关掉兜底：缺 X-User-Id 即 401
    monkeypatch.setattr(config, 'IDENTITY_DEFAULT_ENABLED', False)
    assert client.post("/v1/sessions", headers={"X-Tenant-Id": "1"}).status_code == 401


def test_isolation_between_users(client):
    r1 = client.post("/v1/sessions", headers={"X-Tenant-Id": "1", "X-User-Id": "u1"})
    sid = r1.json()["data"]["session_id"]
    client.post(f"/v1/sessions/{sid}/turns", json={"role": "user", "content": "hi"},
                headers={"X-Tenant-Id": "1", "X-User-Id": "u1"})
    # 用户 u2 没有该会话的注册表行 -> 404（跨用户不可见）
    r2 = client.get(f"/v1/sessions/{sid}", headers={"X-Tenant-Id": "1", "X-User-Id": "u2"})
    assert r2.status_code == 404
    # 用户 u1 能读到
    r3 = client.get(f"/v1/sessions/{sid}", headers={"X-Tenant-Id": "1", "X-User-Id": "u1"})
    assert r3.status_code == 200
    assert r3.json()["data"]["content"][0]["content"] == "hi"
