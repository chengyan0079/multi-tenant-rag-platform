"""提示词缓存：先 Redis 后 Nacos（PROMPT 组），Nacos 不可达时调用方回退内置模板；Redis 不可用降级直连 Nacos。key 前缀/TTL 由 store.redis_keys 管理。"""
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
