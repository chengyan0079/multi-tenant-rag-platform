"""trace_id 注入日志格式：loguru patcher + 中间件端到端。"""
import loguru
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.logger import LOG_FORMAT, logger, trace_id_ctx
from app.middleware import TraceMiddleware


def _capture():
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), format=LOG_FORMAT, level="INFO")
    return lines, sink_id


def test_log_format_includes_trace_from_context() -> None:
    lines, sink_id = _capture()
    try:
        token = trace_id_ctx.set("abc123")
        try:
            logger.info("hello")
        finally:
            trace_id_ctx.reset(token)
        assert any("trace=abc123" in ln for ln in lines)
    finally:
        logger.remove(sink_id)


def test_log_format_default_trace_when_absent() -> None:
    lines, sink_id = _capture()
    try:
        logger.info("no-trace")
        assert any("trace=-" in ln for ln in lines)
    finally:
        logger.remove(sink_id)


def test_middleware_sets_trace_in_logs_and_header() -> None:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/ping")
    def ping():
        logger.info("handling ping")
        return {"ok": True}

    lines, sink_id = _capture()
    try:
        client = TestClient(app)
        # 1) 带 X-Trace-Id：响应头与日志 trace 一致
        r = client.get("/ping", headers={"X-Trace-Id": "req-42"})
        assert r.headers.get("X-Trace-Id") == "req-42"
        assert any("trace=req-42" in ln for ln in lines)

        # 2) 不带：中间件生成，响应头与日志 trace 仍一致
        r2 = client.get("/ping")
        gen = r2.headers.get("X-Trace-Id")
        assert gen
        assert any(f"trace={gen}" in ln for ln in lines)
    finally:
        logger.remove(sink_id)
