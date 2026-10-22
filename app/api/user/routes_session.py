"""会话（多轮，用户侧）（§17.2）

会话由 RAG_ULTRA 服务端生成 session_id，仅绑定 user_id；
同一用户可拥有多个会话，所有读写按 (用户, 会话) 隔离，跨用户不可见。
user_id 由网关 OAuth 后全局唯一下发（跨租户不重复），故无需再叠加 tenant_id。
user_id 由网关 OAUTH 后下发的 X-User-Id 透传，不从请求体传入（防伪造）。

会话实体与会话记忆由独立模块 core.session / core.session_memory 承载。
请求体 / 响应 data 结构统一见 app.api.schemas。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.schemas import (
    OkData, SessionCreated, SessionDetail, SessionListData, SessionItem,
    SessionPatch, TurnRequest,
)
from app.api.deps import default_user_id
from app.app_state import user_id_ctx
from common.response import ApiResponse, success
from core.session.session_service import SessionService
from store.session_store import SessionView

router = APIRouter(prefix="/v1", tags=["session"])


def _require_user(request: Request) -> str:
    """取用户身份：优先 X-User-Id（网关注入），未接网关时兜底为 DEFAULT_USER_ID。

    会话仅按 user_id 隔离，不依赖租户上下文，故直接读请求头，不再经 resolve_tenant。
    兜底关闭（IDENTITY_DEFAULT_ENABLED=False）且缺头时拒绝——网关接入后应如此。
    """
    uid = request.headers.get("X-User-Id") or default_user_id()
    if not uid:
        raise HTTPException(status_code=401, detail="缺少 X-User-Id 请求头")
    user_id_ctx.set(uid)
    return uid


@router.post("/sessions", response_model=ApiResponse[SessionCreated], summary="创建会话")
async def create_session(uid: str = Depends(_require_user)):
    """创建会话：服务端生成 session_id，返回 (session_id, title)。"""
    sid, title = await SessionService().create(uid)
    return success(SessionCreated(session_id=sid, title=title))


@router.get("/sessions", response_model=ApiResponse[SessionListData], summary="会话列表")
async def list_sessions(uid: str = Depends(_require_user)):
    """列出当前用户的全部会话（富字段：title/preview/memory_summary/turn_count...）。"""
    items = await SessionService().list(uid)
    return success(SessionListData(sessions=[_to_item(it) for it in items]))


@router.post("/sessions/{session_id}/turns", response_model=ApiResponse[OkData], summary="追加一轮对话")
async def append_turn(session_id: str, req: TurnRequest, uid: str = Depends(_require_user)):
    """存入一轮对话；首轮自动生成标题；每 4 轮异步触发记忆压缩。"""
    await SessionService().append_turn(uid, session_id, req.role, req.content)
    return success(OkData())


@router.get("/sessions/{session_id}", response_model=ApiResponse[SessionDetail], summary="会话详情")
async def get_session(session_id: str, uid: str = Depends(_require_user)):
    """拉取会话详情：标题 + 原文内容 + 记忆 + 时间。"""
    data = await SessionService().get(uid, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return success(SessionDetail.model_validate(data))


@router.patch("/sessions/{session_id}", response_model=ApiResponse[OkData], summary="更新会话标题")
async def update_session(session_id: str, req: SessionPatch, uid: str = Depends(_require_user)):
    """用户编辑会话标题。"""
    if req.title is not None:
        await SessionService().rename(uid, session_id, req.title)
    return success(OkData())


@router.delete("/sessions/{session_id}", response_model=ApiResponse[OkData], summary="删除会话")
async def delete_session(session_id: str, uid: str = Depends(_require_user)):
    """删除会话：清 Redis 原文/记忆 + MySQL 注册表。"""
    await SessionService().delete(uid, session_id)
    return success(OkData())


def _to_item(v: SessionView) -> SessionItem:
    """会话列表视图 → 响应模型。"""
    return SessionItem.model_validate(v.to_dict())
