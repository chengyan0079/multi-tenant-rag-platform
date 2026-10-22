"""signature - HMAC-SHA256 请求签名的生成与校验（纯函数，不依赖 web 框架）。

约定见 core.auth.__init__ 文档。这里的函数都不碰 Redis / 请求对象，便于单测与被复用
（生成侧的 demo 脚本、校验侧的中间件用的是同一套 canonical，保证不会两端写歪）。
"""
import hashlib
import hmac
import secrets as _secrets
import time
from typing import Mapping, Sequence

HEADER_TIMESTAMP = 'x-auth-timestamp'
HEADER_NONCE = 'x-auth-nonce'
HEADER_SIGNATURE = 'x-auth-signature'

_SEP = '\n'
_HEX_LEN = 64          # sha256 hexdigest 长度，用于快速形态校验


def new_nonce() -> str:
    """生成随机 nonce（防重放用的一次性串）。"""
    return _secrets.token_hex(16)


def now_ts() -> int:
    """当前秒级时间戳（签名侧与校验侧统一取秒）。"""
    return int(time.time())


def canonical_string(method: str, path: str, timestamp: str | int, nonce: str,
                     tenant: str = '', user: str = '') -> str:
    """待签字符串：把"请求目标 + 时效 + 身份"全部纳入，缺一项都算被篡改。

    各字段用换行分隔（字段内不含换行，避免歧义）；tenant/user 为空时留空占位，
    保证"不带身份头"与"带身份头"的签名不会互相套用。
    """
    return _SEP.join([
        (method or '').upper(),
        path or '',
        str(timestamp),
        nonce or '',
        tenant or '',
        user or '',
    ])


def sign(secret: str, method: str, path: str, timestamp: str | int, nonce: str,
         tenant: str = '', user: str = '') -> str:
    """对 canonical 串做 HMAC-SHA256，返回 hex 签名。"""
    canonical = canonical_string(method, path, timestamp, nonce, tenant, user)
    return hmac.new(secret.encode('utf-8'), canonical.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def build_auth_headers(method: str, path: str, secret: str, tenant: str = '',
                       user: str = '', timestamp: int | None = None,
                       nonce: str | None = None) -> dict[str, str]:
    """生成调用方需要的三个头（含身份头），供自测脚本与联调方直接使用。"""
    ts = now_ts() if timestamp is None else timestamp
    nc = new_nonce() if nonce is None else nonce
    return {
        'X-Auth-Timestamp': str(ts),
        'X-Auth-Nonce': nc,
        'X-Auth-Signature': sign(secret, method, path, ts, nc, tenant, user),
    }


def _match_any(secret_or_secrets: str | Sequence[str], method: str, path: str,
               timestamp: str, nonce: str, tenant: str, user: str,
               signature: str) -> bool:
    """按候选密钥逐个比对（支持轮换期多密钥），用 compare_digest 防时序侧信道。"""
    if isinstance(secret_or_secrets, str):
        secret_or_secrets = [secret_or_secrets]
    for sec in secret_or_secrets:
        if not sec:
            continue
        expect = sign(sec, method, path, timestamp, nonce, tenant, user)
        if hmac.compare_digest(expect, signature):
            return True
    return False


def verify(headers: Mapping[str, str], method: str, path: str,
           secrets: str | Sequence[str], ttl: int,
           now: int | None = None) -> str | None:
    """校验签名，返回 None 表示通过，否则返回拒绝原因（便于日志/响应直接说明）。

    校验顺序：头齐全 → 时间戳可解析且在窗口内 → 签名匹配。
    """
    # HTTP 头名大小写不敏感：ASGI 侧拿到的是全小写，调用方构造时常用 X-Auth-* 形式
    h = {str(k).lower(): v for k, v in headers.items()}
    ts_raw = (h.get(HEADER_TIMESTAMP) or '').strip()
    nonce = (h.get(HEADER_NONCE) or '').strip()
    signature = (h.get(HEADER_SIGNATURE) or '').strip().lower()

    if not ts_raw or not nonce or not signature:
        return '缺少签名头（X-Auth-Timestamp / X-Auth-Nonce / X-Auth-Signature）'
    if len(signature) != _HEX_LEN:
        return '签名格式非法'
    try:
        ts = int(ts_raw)
    except ValueError:
        return 'X-Auth-Timestamp 必须为秒级整数'

    base = time.time() if now is None else now
    if abs(base - ts) > ttl:
        return f'签名已过期（偏差 {int(abs(base - ts))}s > {ttl}s）'

    tenant = h.get('x-tenant-id', '') or ''
    user = h.get('x-user-id', '') or ''
    if not _match_any(secrets, method, path, ts_raw, nonce, tenant, user, signature):
        return '签名校验失败'
    return None
