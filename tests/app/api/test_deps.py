"""deps.resolve_tenant 单测：身份头优先，未接网关时兜底为默认租户/用户。"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import default_tenant_id, default_user_id, resolve_tenant
from app.app_state import user_id_ctx
from common.config import config


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()

    @a.get("/who")
    async def who(tid: int = Depends(resolve_tenant)):
        return {'tenant_id': tid, 'user_id': user_id_ctx.get()}

    return a


def test_header_identity_used_when_present(app) -> None:
    r = TestClient(app).get('/who', headers={'X-Tenant-Id': '7', 'X-User-Id': 'u7'})
    assert r.json() == {'tenant_id': 7, 'user_id': 'u7'}


def test_missing_headers_fall_back_to_defaults(app, monkeypatch) -> None:
    monkeypatch.setattr(config, 'IDENTITY_DEFAULT_ENABLED', True)
    monkeypatch.setattr(config, 'DEFAULT_TENANT_ID', 100)
    monkeypatch.setattr(config, 'DEFAULT_USER_ID', '1001')
    assert TestClient(app).get('/who').json() == {'tenant_id': 100, 'user_id': '1001'}


def test_fallback_disabled_requires_tenant(app, monkeypatch) -> None:
    """接入网关后关掉兜底：缺 X-Tenant-Id 即 401（避免漏传头时静默落到默认租户）。"""
    monkeypatch.setattr(config, 'IDENTITY_DEFAULT_ENABLED', False)
    r = TestClient(app).get('/who', headers={'X-User-Id': 'u1'})
    assert r.status_code == 401


def test_non_numeric_tenant_rejected(app) -> None:
    assert TestClient(app).get('/who', headers={'X-Tenant-Id': 'abc'}).status_code == 400


@pytest.mark.parametrize('enabled', [True, False])
def test_default_helpers_reflect_switch(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(config, 'IDENTITY_DEFAULT_ENABLED', enabled)
    monkeypatch.setattr(config, 'DEFAULT_TENANT_ID', 100)
    monkeypatch.setattr(config, 'DEFAULT_USER_ID', '1001')
    assert (default_tenant_id() == 100) is enabled
    assert (default_user_id() == '1001') is enabled


def test_context_not_shared_between_calls(app) -> None:
    """contextvars 不串号：两次不同租户的请求互不影响。"""
    c = TestClient(app)
    assert c.get('/who', headers={'X-Tenant-Id': '1'}).json()['tenant_id'] == 1
    assert c.get('/who', headers={'X-Tenant-Id': '2'}).json()['tenant_id'] == 2
