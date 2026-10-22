"""
test_prompts_cache - prompts_cache 单元测试（pytest + mock）

不依赖真实 Redis / Nacos。通过 mock redis.Redis 与 get_config_center_client 验证：
- get_prompt：先命中 Redis、未命中回源 Nacos 并回填（带 TTL）、Redis 不可用时降级直连
- warmup_prompts：列举并逐条写 Redis（带 TTL）、Redis 不可用时跳过
- refresh_prompt：变更写缓存、删除配置时清缓存
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

import pytest

from store.redis_keys import KeyTTL
import core.config_center.prompts_cache as pc


@pytest.fixture(autouse=True)
def reset_client():
    # 每个用例重置懒加载单例，避免用例间串扰
    pc._sync_r = None
    yield
    pc._sync_r = None


@pytest.fixture
def fake_redis():
    r = mock.MagicMock()
    r.get.return_value = None
    r.set.return_value = True
    r.delete.return_value = True
    return r


@pytest.fixture
def fake_nacos():
    client = mock.MagicMock()
    client.get_config.return_value = "PROMPT_CONTENT"
    client.list_configs.return_value = {
        "totalCount": 2,
        "pageItems": [
            {"dataId": "A", "groupName": "PROMPT"},
            {"dataId": "B", "groupName": "PROMPT"},
        ],
    }
    return client


def patch_redis(fake_r):
    return mock.patch.object(pc._redis, "Redis", return_value=fake_r)


def patch_nacos(fake_c):
    return mock.patch.object(pc, "get_config_center_client", return_value=fake_c)


def assert_set_with_ttl(call_args):
    assert call_args.kwargs.get("ex") == KeyTTL.PROMPT


# ---- get_prompt ----
def test_get_prompt_hit_redis(fake_redis, fake_nacos):
    fake_redis.get.return_value = b"CACHED"
    with patch_redis(fake_redis), patch_nacos(fake_nacos):
        out = pc.get_prompt("X")
    assert out == "CACHED"
    fake_nacos.get_config.assert_not_called()  # 命中 Redis 不回源


def test_get_prompt_miss_then_nacos_and_fill(fake_redis, fake_nacos):
    fake_redis.get.return_value = None
    with patch_redis(fake_redis), patch_nacos(fake_nacos):
        out = pc.get_prompt("X")
    assert out == "PROMPT_CONTENT"
    fake_nacos.get_config.assert_called_once_with("X", pc.ConfigGroup.PROMPT.value)
    fake_redis.set.assert_called_once()  # 回源后回填 Redis
    assert_set_with_ttl(fake_redis.set.call_args)  # 回填带 TTL


def test_get_prompt_redis_unavailable_fallback(fake_redis, fake_nacos):
    # ping 失败 -> _redis_client 返回 None，降级直连 Nacos，不再写 Redis
    bad = mock.MagicMock()
    bad.ping.side_effect = Exception("down")
    with mock.patch.object(pc._redis, "Redis", return_value=bad), patch_nacos(fake_nacos):
        out = pc.get_prompt("X")
    assert out == "PROMPT_CONTENT"
    fake_redis.set.assert_not_called()


# ---- warmup_prompts ----
def test_warmup_writes_all(fake_redis, fake_nacos):
    with patch_redis(fake_redis), patch_nacos(fake_nacos):
        n = pc.warmup_prompts()
    assert n == 2
    assert fake_redis.set.call_count == 2
    for c in fake_redis.set.call_args_list:
        assert_set_with_ttl(c)  # 预热写入带 TTL


def test_warmup_skips_when_redis_down(fake_redis, fake_nacos):
    bad = mock.MagicMock()
    bad.ping.side_effect = Exception("down")
    with mock.patch.object(pc._redis, "Redis", return_value=bad), patch_nacos(fake_nacos):
        n = pc.warmup_prompts()
    assert n == 0  # Redis 不可用时跳过，不报错


# ---- refresh_prompt ----
def test_refresh_writes_when_present(fake_redis, fake_nacos):
    with patch_redis(fake_redis), patch_nacos(fake_nacos):
        pc.refresh_prompt("X")
    fake_redis.set.assert_called_once()
    assert_set_with_ttl(fake_redis.set.call_args)


def test_refresh_deletes_when_gone(fake_redis, fake_nacos):
    fake_nacos.get_config.return_value = None
    with patch_redis(fake_redis), patch_nacos(fake_nacos):
        pc.refresh_prompt("X")
    fake_redis.delete.assert_called_once_with(pc._cache_key("X"))
