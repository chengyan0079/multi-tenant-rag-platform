"""
redis_keys - Redis 键前缀与有效期（TTL）集中管理（store 数据访问层）

所有写入 Redis 的 key 必须经由本模块构造，统一加前缀并设置有效期，
避免键冲突与无限期堆积。store 内部（redis_store）与各业务域
（core/session、core/session_memory、core/config_center/prompts_cache 等）均从这里取前缀与 TTL，
禁止在各自文件里硬编码。

约定（与既有缓存 key 兼容，不做破坏性改名）：
  - 平台级配置（无租户）：rag:prompt:<data_id>
  - 多租户业务：        t:<tenant_id>:<domain>:<rest>
"""
from typing import Final

from common.config import config


class KeyPrefix:
    """各业务域的 key 前缀片段。"""

    PROMPT = ("rag", "prompt")   # 提示词缓存（先 Redis 再 Nacos）：rag:prompt:<data_id>
    TENANT = "t"                 # 多租户隔离段前缀
    ANSWER = "answer"            # 答案缓存
    EMBEDDING = "emb"            # 向量 embedding 缓存
    RATELIMIT = "ratelimit"      # 限流令牌桶
    SESSION = "sess"             # 多轮会话存储
    USER = "u"                  # 用户隔离段前缀（会话按 租户+用户 归属）             # 多轮会话存储
    CHAT_STOP = ("rag", "chatstop")  # 聊天停止标记（取消进行中的流式生成）：rag:chatstop:<user>:<session>
    AUTH_NONCE = ("rag", "authnonce")  # 调用方签名 nonce 去重（防重放）：rag:authnonce:<nonce>


class KeyTTL:
    """各业务域有效期（秒），取值自 common.config（可用环境变量覆盖）。"""

    PROMPT = config.REDIS_TTL_PROMPT
    ANSWER = config.REDIS_TTL_ANSWER
    EMBEDDING = config.REDIS_TTL_EMBEDDING
    RATELIMIT = config.REDIS_TTL_RATELIMIT
    SESSION = config.REDIS_TTL_SESSION
    CHAT_STOP = config.REDIS_TTL_CHAT_STOP
    AUTH_NONCE = config.REDIS_TTL_AUTH_NONCE


def build_key(*parts: str) -> str:
    """用 ':' 连接各段拼装 key。"""
    return ":".join(parts)


def chat_stop_key(user_id: str, session_id: str) -> str:
    """聊天停止标记 key：rag:chatstop:<user_id>:<session_id>。"""
    return build_key(*KeyPrefix.CHAT_STOP, str(user_id), session_id)


def tenant_key(tenant_id: int, *parts: str) -> str:
    """多租户 key：t:<tenant_id>:<parts...>。"""
    return ":".join([KeyPrefix.TENANT, str(tenant_id), *parts])


def prompt_key(data_id: str) -> str:
    """提示词缓存 key：rag:prompt:<data_id>。"""
    return build_key(*KeyPrefix.PROMPT, data_id)


def auth_nonce_key(nonce: str) -> str:
    """签名 nonce 去重 key：rag:authnonce:<nonce>（写入带 TTL，过期自动清理）。"""
    return build_key(*KeyPrefix.AUTH_NONCE, nonce)


# 文档索引任务队列（跨租户，全局，不按 tenant 分段）
INDEX_QUEUE = ("rag", "index_queue")


def index_queue_key() -> str:
    """文档索引任务队列 key：rag:index_queue（IndexManager 消费，崩溃恢复扫描 status=0）。"""
    return build_key(*INDEX_QUEUE)
