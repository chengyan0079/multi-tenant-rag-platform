"""
deps - 请求上下文依赖（M1 多租户 / 鉴权 / traceid）

信任网关经 oauth 下发的上下文头：X-Tenant-Id / X-User-Id。
X-Trace-Id 透传全链路，并在响应头回写。

租户身份由网关在 OAuth 校验后分配，本服务不维护租户表（rag_tenant 已移除）；
角色鉴权也由网关承担，本服务只消费网关下发的租户/用户上下文，不在服务内做角色判断。

未对接 Java 网关前（IDENTITY_DEFAULT_ENABLED=True）：请求未带身份头时兜底为
DEFAULT_TENANT_ID / DEFAULT_USER_ID，方便本地自测；网关接入后关掉该开关即恢复"缺头即拒"。
"""
from fastapi import Depends, Header, HTTPException

from app.app_state import tenant_id_ctx, user_id_ctx
from common.config import config


def default_tenant_id() -> int | None:
    """未接网关时的兜底租户 id；关闭兜底返回 None（此时缺头即 401）。"""
    return config.DEFAULT_TENANT_ID if config.IDENTITY_DEFAULT_ENABLED else None


def default_user_id() -> str | None:
    """未接网关时的兜底用户 id；关闭兜底返回 None（会话等强依赖用户的接口会拒绝）。"""
    return config.DEFAULT_USER_ID if config.IDENTITY_DEFAULT_ENABLED else None


async def resolve_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),  # 租户 id（网关下发）
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),     # 用户 id（网关下发）
) -> int:
    """解析并写入租户/用户上下文，返回租户 id。

    trace_id 由 TraceMiddleware 在请求最外层统一兜底生成并回写响应头，
    此处不再处理，直接透传，避免两层各生成一份导致链路 id 不一致。
    """
    if x_tenant_id:
        try:
            tid = int(x_tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="X-Tenant-Id 必须为整数")
    else:
        tid = default_tenant_id()
        if tid is None:
            raise HTTPException(status_code=401, detail="缺少 X-Tenant-Id 请求头")

    tenant_id_ctx.set(tid)
    user_id_ctx.set(x_user_id or default_user_id())
    return tid
