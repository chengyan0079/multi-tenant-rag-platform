"""中间件三件套：AuthSignatureMiddleware（HMAC-SHA256 校验，防伪造 X-Tenant-Id/User-Id，失败返 401）/ TraceMiddleware（缺失 trace 则生成并回写 X-Trace-Id）/ ApiLogMiddleware（采集请求与响应合并落库，敏感头脱敏、流式与大文件不拦）。均纯 ASGI 不阻塞事件循环。"""
import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.app_state import app_state, trace_id_ctx
from common.config import config
from common.logger import logger
from common.response import ErrCode, fail
from core.auth.signature import verify as verify_signature
from store.redis_keys import KeyTTL, auth_nonce_key

# 记录请求头时需脱敏的敏感头
_SENSITIVE_HEADERS = frozenset({
    'authorization', 'cookie', 'set-cookie', 'proxy-authorization', 'x-api-key',
    'x-auth-signature',          # 签名虽有时效与 nonce，仍不落日志/库
})
_BODY_METHODS = frozenset({'POST', 'PUT', 'PATCH'})


class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Any:
        tid = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        token = trace_id_ctx.set(tid)
        try:
            response = await call_next(request)
        finally:
            # call_next 抛异常时也要复位，否则 contextvar 泄漏到后续请求
            trace_id_ctx.reset(token)
        response.headers["X-Trace-Id"] = tid
        return response


def _excluded(path: str, prefixes: list[str] | None) -> bool:
    """路径是否命中排除前缀（探活/文档等高频或不需记录的调用）。"""
    return any(path.startswith(p) for p in (prefixes or []))


def _compact(v: Any) -> str:
    """紧凑 JSON（日志单行输出 header 等结构，失败回退 str）。"""
    try:
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    except Exception:  # noqa: BLE001
        return str(v)


def _headers_dict(scope: dict, mask: bool = True) -> dict[str, str]:
    """ASGI headers -> dict（小写 key）；mask=True 时敏感头脱敏为 ***。

    验签要用原始值，故 AuthSignatureMiddleware 传 mask=False；日志/落库一律用脱敏后的。
    """
    out: dict[str, str] = {}
    for raw_key, raw_val in scope.get('headers') or []:
        key = raw_key.decode('latin-1').lower()
        out[key] = '***' if (mask and key in _SENSITIVE_HEADERS) else raw_val.decode('latin-1')
    return out


def _caller(scope: dict, headers: dict[str, str]) -> str:
    """调用方：优先 X-Forwarded-For 首段（经网关），回退 client host。"""
    xff = headers.get('x-forwarded-for', '')
    if xff:
        return xff.split(',')[0].strip()
    client = scope.get('client')
    return client[0] if client else ''


def _to_int(v: str | None) -> int | None:
    """租户等数字型头转 int，非法/空返回 None。"""
    try:
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _tenant_of(headers: dict[str, str]) -> int | None:
    """日志/落库用的租户：取请求头，未接网关时按默认值兜底（与 deps 解析结果保持一致）。"""
    tid = _to_int(headers.get('x-tenant-id'))
    if tid is None and config.IDENTITY_DEFAULT_ENABLED:
        return config.DEFAULT_TENANT_ID
    return tid


def _user_of(headers: dict[str, str]) -> str | None:
    """日志/落库用的用户：取请求头，未接网关时按默认值兜底。"""
    return headers.get('x-user-id') or (
        config.DEFAULT_USER_ID if config.IDENTITY_DEFAULT_ENABLED else None)


class AuthSignatureMiddleware:
    """调用方签名校验（纯 ASGI）：未通过直接 401，不进路由。

    校验 = 签名/时间窗（core.auth.signature.verify）+ nonce 去重（Redis SETNX）。
    密钥缺失或 Redis 不可用时默认降级放行并各告警一次；AUTH_HMAC_FAIL_CLOSED=True
    时改为拒绝请求，避免安全敏感部署在依赖故障时静默失去防重放能力。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._no_secret_warned = False
        self._no_redis_warned = False

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get('type') != 'http' or not config.AUTH_HMAC_ENABLED:
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if _excluded(path, config.AUTH_HMAC_EXCLUDE):
            await self.app(scope, receive, send)
            return

        secrets = [s for s in (config.AUTH_HMAC_SECRET, config.AUTH_HMAC_SECRET_PREV) if s]
        if not secrets:
            # 开了开关却没配密钥：等同未开启，告警一次，避免"以为在防护其实没有"
            if not self._no_secret_warned:
                self._no_secret_warned = True
                logger.warning('AUTH_HMAC_ENABLED=True 但未配置 AUTH_HMAC_SECRET，签名校验实际未生效')
            if config.AUTH_HMAC_FAIL_CLOSED:
                await self._unauthorized(send, '服务未配置签名密钥')
                return
            await self.app(scope, receive, send)
            return

        method = scope.get('method', '').upper()
        headers = _headers_dict(scope, mask=False)
        reason = verify_signature(headers, method, path, secrets, config.AUTH_HMAC_TTL)
        if reason is None:
            reason = await self._consume_nonce(headers.get('x-auth-nonce', ''))
        if reason is not None:
            logger.warning(
                f'调用方签名校验未通过: {method} {path} caller={_caller(scope, headers)} '
                f'reason={reason}')
            await self._unauthorized(send, reason)
            return
        await self.app(scope, receive, send)

    async def _consume_nonce(self, nonce: str) -> str | None:
        """占用 nonce（一次性）；返回 None 表示可用，否则返回拒绝原因。"""
        r = app_state.redis
        if r is None:
            self._warn_redis_once()
            return 'nonce 去重不可用' if config.AUTH_HMAC_FAIL_CLOSED else None
        try:
            ok = await r.set(auth_nonce_key(nonce), '1', nx=True, ex=KeyTTL.AUTH_NONCE)
        except Exception as e:  # noqa: BLE001  Redis 故障默认降级，仅时间窗；fail-closed 时拒绝
            if not self._no_redis_warned:
                logger.warning(f'nonce 去重失败，降级为仅校验时间窗: {e}')
                self._no_redis_warned = True
            return 'nonce 去重不可用' if config.AUTH_HMAC_FAIL_CLOSED else None
        return None if ok else 'nonce 已使用（疑似重放）'

    def _warn_redis_once(self) -> None:
        if not self._no_redis_warned:
            self._no_redis_warned = True
            logger.warning('Redis 不可用，签名 nonce 去重降级为仅校验时间窗')

    @staticmethod
    async def _unauthorized(send: Callable, reason: str) -> None:
        """返回 401 统一响应（中间件层无法走全局异常处理器，直接构造响应）。"""
        body = fail(ErrCode.UNAUTHORIZED, f'调用方签名校验未通过: {reason}').model_dump_json().encode('utf-8')
        await send({
            'type': 'http.response.start',
            'status': 401,
            'headers': [
                (b'content-type', b'application/json; charset=utf-8'),
                (b'content-length', str(len(body)).encode('ascii')),
            ],
        })
        await send({'type': 'http.response.body', 'body': body})


async def _capture_request_body(scope: dict, receive: Callable):
    """捕获请求体文本，并返回可重放的 receive（保证下游仍读到完整 body）。

    返回 (body_text, receive)。multipart（文件上传）与超大体不消费、只留说明文字。
    """
    headers = _headers_dict(scope)
    if scope.get('method', '').upper() not in _BODY_METHODS:
        return '', receive
    if 'multipart/form-data' in headers.get('content-type', ''):
        return '<multipart 请求体未记录>', receive
    try:
        length = int(headers.get('content-length') or 0)
    except ValueError:
        length = 0
    if length > config.API_LOG_MAX_BODY:
        return f'<请求体过大未记录: {length} bytes>', receive

    # 消费全部 http.request 消息并缓存，随后原样重放（否则下游读不到 body）
    messages: list[dict] = []
    raw = b''
    while True:
        msg = await receive()
        messages.append(msg)
        if msg['type'] != 'http.request':
            break
        raw += msg.get('body', b'')
        if not msg.get('more_body', False):
            break
    idx = 0

    async def replay() -> dict:
        nonlocal idx
        if idx < len(messages):
            msg = messages[idx]
            idx += 1
            return msg
        return {'type': 'http.disconnect'}

    max_len = config.API_LOG_MAX_BODY
    text = raw[:max_len].decode('utf-8', 'replace')
    if len(raw) > max_len:
        text += '...<截断>'
    return text, replay


class ApiLogMiddleware:
    """API 调用日志中间件（纯 ASGI）：请求/响应各打一条日志，合并落一行到数据库。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get('type') != 'http' or not config.API_LOG_ENABLED:
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if _excluded(path, config.API_LOG_EXCLUDE):
            await self.app(scope, receive, send)
            return

        method = scope.get('method', '').upper()
        headers = _headers_dict(scope)
        caller = _caller(scope, headers)
        tid = headers.get('x-trace-id') or trace_id_ctx.get()
        query = scope.get('query_string', b'').decode('utf-8', 'replace')
        req_at = datetime.now()
        req_body, receive = await _capture_request_body(scope, receive)

        logger.info(
            f'API 请求 trace={tid} {method} {path} caller={caller} '
            f'tenant={_tenant_of(headers) or ""} user={_user_of(headers) or ""} '
            f'query={query} headers={_compact(headers)} body={req_body}'
        )

        status: dict[str, Any] = {}
        chunks: list[bytes] = []
        total = 0
        max_body = config.API_LOG_MAX_BODY

        async def send_wrapper(message: dict) -> None:
            nonlocal total
            if message['type'] == 'http.response.start':
                status['code'] = message.get('status')
            elif message['type'] == 'http.response.body':
                chunk = message.get('body', b'')
                if total < max_body:
                    chunks.append(chunk)
                    total += len(chunk)
                # 始终透传：不缓冲、不阻塞流式（SSE）响应
            await send(message)

        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            cost_ms = int((time.perf_counter() - t0) * 1000)
            resp_at = datetime.now()
            raw = b''.join(chunks)
            resp_body = raw.decode('utf-8', 'replace')
            if total >= max_body:
                resp_body += '...<截断>'
            status_code = status.get('code')

            logger.info(
                f'API 响应 trace={tid} {method} {path} status={status_code} '
                f'cost_ms={cost_ms} body={resp_body}'
            )

            if config.API_LOG_DB:
                from store.api_log_dao import ApiLogDao
                await ApiLogDao().save({
                    'trace_id': tid,
                    'caller': caller,
                    'user_id': _user_of(headers),
                    'tenant_id': _tenant_of(headers),
                    'method': method,
                    'path': path,
                    'route': getattr(scope.get('route'), 'path', None),
                    'query_string': query,
                    'req_headers': headers,
                    'req_body': req_body,
                    'req_time': req_at,
                    'status_code': status_code,
                    'resp_body': resp_body,
                    'resp_time': resp_at,
                    'cost_ms': cost_ms,
                })
