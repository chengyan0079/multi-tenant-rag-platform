"""CORS 中间件与配置化白名单单测（add_cors + Config._split_csv）。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import add_cors
from common.config import Config, config


def test_add_cors_allows_only_configured_origin(monkeypatch):
    monkeypatch.setattr(config, "CORS_ALLOW_ORIGINS", ["https://x.com"])
    monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", False)

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    add_cors(app)
    client = TestClient(app)

    allowed = client.get("/ping", headers={"Origin": "https://x.com"})
    assert allowed.headers.get("access-control-allow-origin") == "https://x.com"

    denied = client.get("/ping", headers={"Origin": "https://evil.com"})
    assert denied.headers.get("access-control-allow-origin") is None


def test_cors_origins_accepts_csv_and_json() -> None:
    csv = Config(CORS_ALLOW_ORIGINS="https://a.com,https://b.com").CORS_ALLOW_ORIGINS
    assert csv == ["https://a.com", "https://b.com"]
    js = Config(CORS_ALLOW_ORIGINS='["https://a.com", "https://b.com"]').CORS_ALLOW_ORIGINS
    assert js == ["https://a.com", "https://b.com"]


def test_cors_declared_default_is_empty() -> None:
    # 声明默认不放行任何跨域源（不实例化，避免读到 .env 覆盖）
    assert Config.model_fields["CORS_ALLOW_ORIGINS"].default == []


def test_cors_origins_parses_bracket_forms() -> None:
    assert Config(CORS_ALLOW_ORIGINS="[*]").CORS_ALLOW_ORIGINS == ["*"]
    assert Config(CORS_ALLOW_ORIGINS="[a.com,b.com]").CORS_ALLOW_ORIGINS == ["a.com", "b.com"]
    assert Config(CORS_ALLOW_ORIGINS='["a.com", "b.com"]').CORS_ALLOW_ORIGINS == ["a.com", "b.com"]
