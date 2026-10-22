"""
redis_store - Redis 缓存（答案缓存 / 向量 embedding 缓存 / 限流令牌桶）（M3）

所有 key 强制加 t:{tenant_id}: 前缀（前缀与 TTL 统一由 store.redis_keys 管理），确保多租户隔离。
"""
import hashlib
import json

from common.config import config
from common.logger import logger
from app.app_state import app_state
from store.redis_keys import KeyTTL, chat_stop_key, tenant_key


def _key(tenant_id: int, *parts: str) -> str:
    return tenant_key(tenant_id, *parts)


def _normalize_query(query: str) -> str:
    return hashlib.md5(query.strip().lower().encode('utf-8')).hexdigest()


async def get_answer(tenant_id: int, query: str) -> str | None:
    """答案缓存命中（按归一化 query hash）。"""
    if app_state.redis is None:
        return None
    raw = await app_state.redis.get(_key(tenant_id, 'answer', _normalize_query(query)))
    return raw


async def set_answer(tenant_id: int, query: str, answer: str, ttl: int = KeyTTL.ANSWER):
    if app_state.redis is None:
        return
    try:
        await app_state.redis.set(_key(tenant_id, 'answer', _normalize_query(query)), answer, ex=ttl)
    except Exception as e:
        logger.warning(f'答案缓存写入失败: {e}')


async def get_cached_embedding(tenant_id: int, text: str) -> dict | None:
    """embedding 缓存（dense + sparse 序列化）。"""
    if app_state.redis is None:
        return None
    raw = await app_state.redis.get(_key(tenant_id, 'emb', _normalize_query(text)))
    return json.loads(raw) if raw else None


async def set_cached_embedding(tenant_id: int, text: str, embedding: dict, ttl: int = KeyTTL.EMBEDDING):
    if app_state.redis is None:
        return
    try:
        # sparse 为 dict[int,float]，json 序列化需将 key 转 str
        payload = {
            'dense': embedding['dense'],
            'sparse': {str(k): v for k, v in embedding['sparse'].items()},
        }
        await app_state.redis.set(_key(tenant_id, 'emb', _normalize_query(text)), json.dumps(payload), ex=ttl)
    except Exception as e:
        logger.warning(f'embedding 缓存写入失败: {e}')


async def acquire_token(tenant_id: int, tokens: int = 1, capacity: int = 100, rate: int = 50) -> bool:
    """简单令牌桶限流（Redis INCR + EXPIRE 近似实现）。返回是否放行。"""
    if app_state.redis is None:
        return True
    key = _key(tenant_id, 'ratelimit')
    try:
        cur = await app_state.redis.incr(key)
        if cur == 1:
            await app_state.redis.expire(key, KeyTTL.RATELIMIT)
        return cur <= capacity
    except Exception:
        return True


async def ping() -> bool:
    try:
        return bool(await app_state.redis.ping()) if app_state.redis else False
    except Exception:
        return False


# ---- 聊天停止标记（跨 worker 取消进行中的流式生成）----
async def set_chat_stop(user_id: str, session_id: str) -> None:
    """置停止标记（短 TTL 自动过期）。"""
    if app_state.redis is None:
        return
    try:
        await app_state.redis.set(chat_stop_key(user_id, session_id), "1", ex=KeyTTL.CHAT_STOP)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'聊天停止标记写入失败: {e}')


async def is_chat_stopped(user_id: str, session_id: str) -> bool:
    """是否已请求停止。"""
    if app_state.redis is None:
        return False
    try:
        return bool(await app_state.redis.get(chat_stop_key(user_id, session_id)))
    except Exception:
        return False


async def clear_chat_stop(user_id: str, session_id: str) -> None:
    """清除停止标记（进入/结束时调用，避免残留误停）。"""
    if app_state.redis is None:
        return
    try:
        await app_state.redis.delete(chat_stop_key(user_id, session_id))
    except Exception:
        pass
