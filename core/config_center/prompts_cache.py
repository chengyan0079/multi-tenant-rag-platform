"""
prompts_cache - 提示词缓存（先 Redis 再 Nacos）

读取链路（自上而下回源）：
  1. Redis：进程外共享缓存。启动时由 NacosConfigManager 预热写入；配置经审批流变更后主动刷新。
  2. Nacos 配置中心（PROMPT 组，namespace=rag-ultra）Open API：持久化源。
Nacos 不可达或配置缺失时，调用方应自行回退到内置模板。

实现要点：
- 同步接口：Redis 用 redis-py 同步客户端，便于在同步/异步上下文中都能直接调用
  （与项目既有的 redis.asyncio 即 app_state.redis 用于答案缓存等并存，不冲突）。
- Redis 不可用时自动降级为直连 Nacos，不阻断业务。
- Redis key 带前缀且设有有效期（TTL），前缀与 TTL 统一由 store.redis_keys 管理。
"""
import json
from typing import Optional

import redis as _redis

from common.config import config as app_config
from common.logger import logger
from core.config_center.groups import ConfigGroup
from core.config_center.nacos_config import ConfigCenterError, get_config_center_client
from store.redis_keys import KeyTTL, prompt_key

_sync_r: "_redis.Redis | None" = None


def _redis_client() -> "_redis.Redis | None":
    """懒加载同步 Redis 客户端；未配置或不可用返回 None（降级直连 Nacos）。"""
    global _sync_r
    if _sync_r is not None:
        return _sync_r
    try:
        _sync_r = _redis.Redis(
            host=app_config.REDIS_HOST,
            port=app_config.REDIS_PORT,
            db=app_config.REDIS_DB,
            password=app_config.REDIS_PASSWORD or None,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _sync_r.ping()
    except Exception as e:  # noqa: BLE001
        logger.warning(f'提示词缓存：同步 Redis 不可用，降级直连 Nacos: {e}')
        _sync_r = None
    return _sync_r


def _cache_key(data_id: str) -> str:
    return prompt_key(data_id)


def get_prompt(data_id: str, group: str = ConfigGroup.PROMPT.value) -> Optional[str]:
    """取提示词内容：先 Redis，未命中再回源 Nacos；回源成功后回填 Redis（带 TTL）。

    返回纯文本模板内容；Nacos 无此配置或读取失败返回 None。
    """
    r = _redis_client()
    if r is not None:
        try:
            cached = r.get(_cache_key(data_id))
            if cached is not None:
                return cached.decode('utf-8') if isinstance(cached, bytes) else cached
        except Exception as e:  # noqa: BLE001
            logger.warning(f'提示词缓存：Redis 读取失败，回退 Nacos: {e}')

    try:
        content = get_config_center_client().get_config(data_id, group)
    except ConfigCenterError as e:
        logger.warning(f'提示词缓存：Nacos 读取失败 data_id={data_id}: {e}')
        content = None

    if content is not None and r is not None:
        try:
            r.set(_cache_key(data_id), content, ex=KeyTTL.PROMPT)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'提示词缓存：Redis 回填失败 data_id={data_id}: {e}')
    return content


def get_prompt_json(data_id: str, group: str = ConfigGroup.PROMPT.value) -> Optional[dict]:
    """提示词内容为 JSON 时解析为 dict；非 JSON（纯文本模板）返回 None。"""
    content = get_prompt(data_id, group)
    if content is None:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None


def warmup_prompts(group: str = ConfigGroup.PROMPT.value) -> int:
    """启动预热：把某 group 下所有配置拉取并写入 Redis（带 TTL）。返回预热条数。

    Redis 不可用时跳过（运行时 get_prompt 会直连 Nacos，功能不中断）。
    """
    r = _redis_client()
    if r is None:
        logger.warning('提示词预热：同步 Redis 不可用，跳过（运行时将直连 Nacos）')
        return 0
    try:
        items = (get_config_center_client().list_configs(group=group, page_size=200)
                 .get('pageItems') or [])
    except ConfigCenterError as e:
        logger.warning(f'提示词预热：Nacos 列举失败: {e}')
        return 0
    n = 0
    for it in items:
        did = it.get('dataId')
        if not did:
            continue
        try:
            content = get_config_center_client().get_config(did, group)
            if content is None:
                continue
            r.set(_cache_key(did), content, ex=KeyTTL.PROMPT)
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f'提示词预热：单条失败 data_id={did}: {e}')
    logger.info(f'提示词预热完成：group={group} 共 {n} 条')
    return n


def refresh_prompt(data_id: str, group: str = ConfigGroup.PROMPT.value) -> None:
    """配置经审批流变更后，主动刷新该提示词的 Redis 缓存（新增/修改写入带 TTL，删除则清除）。"""
    r = _redis_client()
    if r is None:
        return
    try:
        content = get_config_center_client().get_config(data_id, group)
        if content is None:
            r.delete(_cache_key(data_id))
        else:
            r.set(_cache_key(data_id), content, ex=KeyTTL.PROMPT)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'提示词刷新失败 data_id={data_id}: {e}')


def refresh_all_prompts(group: str = ConfigGroup.PROMPT.value) -> int:
    """刷新某 group 下全部提示词（等价于重新预热）。"""
    return warmup_prompts(group)
