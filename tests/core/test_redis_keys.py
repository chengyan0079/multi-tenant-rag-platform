"""
test_redis_keys - 集中前缀/TTL 管理单测（pytest + mock）

验证 key 构造与各业务域前缀、TTL 定义。不依赖真实 Redis。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from store.redis_keys import KeyPrefix, KeyTTL, build_key, tenant_key, prompt_key


def test_prefix_and_ttl_defined():
    assert KeyPrefix.PROMPT == ("rag", "prompt")
    assert KeyPrefix.TENANT == "t"
    assert KeyPrefix.ANSWER == "answer"
    assert KeyPrefix.EMBEDDING == "emb"
    assert KeyPrefix.RATELIMIT == "ratelimit"
    assert KeyPrefix.SESSION == "sess"
    assert KeyTTL.PROMPT == 3600
    assert KeyTTL.ANSWER == 3600
    assert KeyTTL.EMBEDDING == 86400
    assert KeyTTL.RATELIMIT == 1
    assert KeyTTL.SESSION == 1800


def test_build_key_joins_with_colon():
    assert build_key("a", "b", "c") == "a:b:c"


def test_tenant_key_format():
    assert tenant_key(7, "answer", "hash") == "t:7:answer:hash"


def test_prompt_key_format_compat():
    # 与既有缓存 key 兼容：rag:prompt:<data_id>
    assert prompt_key("CORE_RAG_TEMPLATE") == "rag:prompt:CORE_RAG_TEMPLATE"
