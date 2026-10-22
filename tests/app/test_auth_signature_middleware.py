"""AuthSignatureMiddleware 单测：开关、放行/拦截、重放、Redis 降级、排除路径。"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_state import app_state
from app.middleware import AuthSignatureMiddleware
from common.config import config
from core.auth.signature import build_auth_headers

SECRET = 'devsecret'


class _FakeRedis:
    """只实现中间件用到的 SETNX：key 已存在返回 False（判定为重放）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        self.calls.append((key, value, nx, ex))
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True


class _BrokenRedis:
    async def set(self, *a, **kw) -> bool:
        raise RuntimeError('redis down')


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthSignatureMiddleware)

    @app.get('/ping')
    async def ping():
        return {'ok': True}

    @app.get('/admin/health')
    async def health():
        return {'status': 'ok'}

    return app


@pytest.fixture
def _auth_on(monkeypatch):
    """开启验签并挂上假 Redis；用例结束还原全局状态。"""
    monkeypatch.setattr(config, 'AUTH_HMAC_ENABLED', True)
    monkeypatch.setattr(config, 'AUTH_HMAC_SECRET', SECRET)
    monkeypatch.setattr(config, 'AUTH_HMAC_SECRET_PREV', '')
    monkeypatch.setattr(config, 'AUTH_HMAC_TTL', 60)
    redis = _FakeRedis()
    monkeypatch.setattr(app_state, 'redis', redis)
    yield redis
    app_state.redis = None


def _signed(method: str, path: str, tenant: str = '1', user: str = 'u1',
            secret: str = SECRET, ts: int | None = None, nonce: str | None = None) -> dict:
    headers = build_auth_headers(method, path, secret, tenant=tenant, user=user,
                                 timestamp=ts, nonce=nonce)
    return {**headers, 'X-Tenant-Id': tenant, 'X-User-Id': user}


def test_disabled_passes_without_signature(monkeypatch) -> None:
    monkeypatch.setattr(config, 'AUTH_HMAC_ENABLED', False)
    assert TestClient(_make_app()).get('/ping').status_code == 200


def test_enabled_requires_signature(_auth_on) -> None:
    r = TestClient(_make_app()).get('/ping', headers={'X-Tenant-Id': '1'})
    assert r.status_code == 401
    assert r.json()['code'] == 40100
    assert '缺少签名头' in r.json()['msg']


def test_valid_signature_passes(_auth_on) -> None:
    headers = _signed('GET', '/ping')
    assert TestClient(_make_app()).get('/ping', headers=headers).status_code == 200
    key, _, nx, _ex = _auth_on.calls[0]
    assert key.startswith('rag:authnonce:')      # key 走统一前缀，且带 TTL
    assert nx is True                            # SETNX：已存在即判重放


def test_wrong_signature_rejected(_auth_on) -> None:
    r = TestClient(_make_app()).get('/ping', headers=_signed('GET', '/ping', secret='wrong'))
    assert r.status_code == 401
    assert '签名校验失败' in r.json()['msg']


def test_expired_signature_rejected(_auth_on) -> None:
    old = int(time.time()) - 600
    r = TestClient(_make_app()).get('/ping', headers=_signed('GET', '/ping', ts=old))
    assert r.status_code == 401
    assert '过期' in r.json()['msg']


def test_tampered_identity_rejected(_auth_on) -> None:
    """签名针对 tenant=1 生成，请求里改成 2 → 拒绝（这是本机制的核心）。"""
    headers = _signed('GET', '/ping', tenant='1')
    headers['X-Tenant-Id'] = '2'
    assert TestClient(_make_app()).get('/ping', headers=headers).status_code == 401


def test_replay_rejected(_auth_on) -> None:
    """同一 nonce 用第二次即视为重放。"""
    client = TestClient(_make_app())
    headers = _signed('GET', '/ping')
    assert client.get('/ping', headers=headers).status_code == 200
    r = client.get('/ping', headers=headers)
    assert r.status_code == 401
    assert 'nonce' in r.json()['msg']


def test_excluded_path_passes(_auth_on) -> None:
    assert TestClient(_make_app()).get('/admin/health').status_code == 200


def test_redis_unavailable_degrades_to_time_window(_auth_on, monkeypatch) -> None:
    """Redis 挂了不能把请求全打死：降级为只校验签名与时间窗。"""
    monkeypatch.setattr(app_state, 'redis', _BrokenRedis())
    assert TestClient(_make_app()).get('/ping', headers=_signed('GET', '/ping')).status_code == 200


def test_enabled_without_secret_warns_and_passes(monkeypatch) -> None:
    """开了开关但没配密钥：等同未开启（告警一次），不静默拦掉所有流量。"""
    monkeypatch.setattr(config, 'AUTH_HMAC_ENABLED', True)
    monkeypatch.setattr(config, 'AUTH_HMAC_SECRET', '')
    monkeypatch.setattr(config, 'AUTH_HMAC_SECRET_PREV', '')
    assert TestClient(_make_app()).get('/ping').status_code == 200


def test_old_secret_still_accepted(_auth_on, monkeypatch) -> None:
    """轮换期：用旧密钥签的名仍应通过。"""
    monkeypatch.setattr(config, 'AUTH_HMAC_SECRET_PREV', 'oldsecret')
    headers = _signed('GET', '/ping', secret='oldsecret')
    assert TestClient(_make_app()).get('/ping', headers=headers).status_code == 200
