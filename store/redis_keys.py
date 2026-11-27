"""Redis 键前缀与 TTL 集中管理：所有写 Redis 的 key 必须经由本模块构造（统一前缀+有效期），禁止硬编码。平台级 rag:prompt:<data_id>；多租户 t:<tenant_id>:<domain>:<rest>。"""
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
    INDEX_RECOVER = ("rag", "idx_recover")  # 索引恢复认领锁（多 worker 避免重复重投）


class KeyTTL:
    """各业务域有效期（秒），取值自 common.config（可用环境变量覆盖）。"""

    PROMPT = config.REDIS_TTL_PROMPT
    ANSWER = config.REDIS_TTL_ANSWER
    EMBEDDING = config.REDIS_TTL_EMBEDDING
    RATELIMIT = config.REDIS_TTL_RATELIMIT
    SESSION = config.REDIS_TTL_SESSION
    CHAT_STOP = config.REDIS_TTL_CHAT_STOP
    AUTH_NONCE = config.REDIS_TTL_AUTH_NONCE
    INDEX_RECOVER = config.REDIS_TTL_INDEX_RECOVER


def build_key(*parts: str) -> str:
    """统一用 ':' 拼接，所有 key 的段序由此函数约定，避免各处手写漂移。"""
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
    """文档索引任务队列 key：rag:index_queue（IndexManager 消费，崩溃恢复扫描 status∈{0,1}）。"""
    return build_key(*INDEX_QUEUE)


def index_recover_key(doc_id: int) -> str:
    """索引恢复认领锁：rag:idx_recover:<doc_id>，避免多 worker 重复重投同一任务。"""
    return build_key(*KeyPrefix.INDEX_RECOVER, str(doc_id))
