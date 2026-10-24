"""ApiLogMiddleware 单测：请求/响应分条日志、请求体捕获后下游仍可读、排除路径、敏感头脱敏。

用 TestClient + 假 logger 收集日志，不依赖数据库（单测关闭落库）。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.middleware as m


class _Rec:
    """替代 logger，收集日志文本。"""

    def __init__(self) -> None:
        self.msgs: list[str] = []

    def info(self, msg, *a, **kw) -> None:
        self.msgs.append(str(msg))

    def exception(self, msg, *a, **kw) -> None:
        self.msgs.append(str(msg))


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get('/ping')
    async def ping():
        return {'ok': True}

    @app.post('/echo')
    async def echo(payload: dict):
        return {'got': payload}

    app.add_middleware(m.ApiLogMiddleware)
    return app


def _setup(monkeypatch) -> _Rec:
    rec = _Rec()
    monkeypatch.setattr(m, 'logger', rec)
    monkeypatch.setattr(m.config, 'API_LOG_EXCLUDE', [])
    monkeypatch.setattr(m.config, 'API_LOG_DB', False)   # 单测不落库
    return rec


def test_request_and_response_logged_separately(monkeypatch) -> None:
    rec = _setup(monkeypatch)
    r = TestClient(_make_app()).get('/ping')
    assert r.status_code == 200
    assert any('API 请求' in x and 'GET /ping' in x for x in rec.msgs)
    assert any('API 响应' in x and 'status=200' in x and 'cost_ms=' in x for x in rec.msgs)


def test_post_body_captured_and_downstream_still_reads(monkeypatch) -> None:
    """核心：中间件读了请求体后必须重放，业务代码仍能拿到完整 body。"""
    rec = _setup(monkeypatch)
    r = TestClient(_make_app()).post('/echo', json={'a': 1, 'b': 'x'})
    assert r.json() == {'got': {'a': 1, 'b': 'x'}}          # 重放生效
    assert any('body=' in x and '"a"' in x for x in rec.msgs)  # 请求体已入日志


def test_excluded_path_not_logged(monkeypatch) -> None:
    rec = _setup(monkeypatch)
    monkeypatch.setattr(m.config, 'API_LOG_EXCLUDE', ['/ping'])
    assert TestClient(_make_app()).get('/ping').status_code == 200
    assert rec.msgs == []


def test_sensitive_header_masked(monkeypatch) -> None:
    rec = _setup(monkeypatch)
    r = TestClient(_make_app()).get(
        '/ping', headers={'Authorization': 'Bearer secret', 'X-Tenant-Id': '7'})
    assert r.status_code == 200
    joined = ' '.join(rec.msgs)
    assert 'Bearer secret' not in joined
    assert '***' in joined
    assert 'tenant=7' in joined
