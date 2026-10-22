"""问答 / 对话（用户侧，SSE 流式）（§17.1）

端用户提问入口：先经网关 OAUTH 拿到 tenant_id / user_id，再调用本接口。
流程：缓存前置 → FAQ 优先 → 意图分流 + 检索增强 + 生成，全程以 SSE 流式输出。

接口：
- POST   /v1/chat                      对话主入口（SSE 流式）
- POST   /v1/chat/stop                 停止当前会话的流式生成（Redis 取消标记，跨 worker 生效）
- DELETE /v1/chat/context/{session_id} 删除当前会话窗口记忆（压缩上下文）

SSE 事件协议（data 均为 JSON 字符串）：
- meta   ：{"source": cache|faq|rag|empty, "intent": ..., "session_id": ..., "chunks": [...]}
- answer ：整段答案（缓存 / FAQ / 空 场景）
- token  ：RAG 逐 token 文本
- stopped：已被停止
- done   ：结束

会话历史按 (用户, 会话) 隔离回写（会话仅按 user_id 隔离，不挂 tenant_id）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import resolve_tenant
from app.api.schemas import ChatRequest, OkData, StopRequest
from app.app_state import user_id_ctx
from common.logger import logger
from common.response import ApiResponse, success
from core.qa_router.router import QARouter
from core.session.session_service import SessionService
from store.redis_store import clear_chat_stop, is_chat_stopped, set_chat_stop

router = APIRouter(prefix="/v1", tags=["chat"])


def _require_user(request: Request) -> str:
    """取网关下发的 X-User-Id（停止/删上下文需要按用户定位）；缺则拒绝。"""
    uid = request.headers.get("X-User-Id")
    if not uid:
        raise HTTPException(status_code=401, detail="缺少 X-User-Id 请求头")
    user_id_ctx.set(uid)
    return uid


@router.post("/chat", summary="对话问答（SSE 流式）")
async def chat(req: ChatRequest, tenant_id: int = Depends(resolve_tenant)):
    """对话主入口（SSE 流式）：FAQ 优先 + RAG 兜底，按 (用户) 隔离写入会话历史。"""
    user_id = user_id_ctx.get()
    router_ = QARouter()
    # 进入前清掉可能残留的停止标记，避免误停本次生成
    if user_id and req.session_id:
        await clear_chat_stop(user_id, req.session_id)

    async def event_gen():
        source = None
        full_answer: list[str] = []
        stopped = False
        try:
            async for event, payload in router_.answer_stream(
                query=req.query, tenant_id=tenant_id, user_id=user_id,
                session_id=req.session_id,
            ):
                # 停止检查：每个事件产出前检查取消标记（Redis，跨 worker 生效）
                if user_id and req.session_id and await is_chat_stopped(user_id, req.session_id):
                    stopped = True
                    break
                if event == "meta":
                    source = payload.get("source")
                elif event in ("answer", "token"):
                    full_answer.append(payload if isinstance(payload, str) else "")
                yield {"event": event, "data": json.dumps(payload, ensure_ascii=False)}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"对话流式输出异常: {e}")
            yield {"event": "error", "data": json.dumps({"msg": "服务内部错误"}, ensure_ascii=False)}
            return
        finally:
            if user_id and req.session_id:
                await clear_chat_stop(user_id, req.session_id)

        if stopped:
            yield {"event": "stopped", "data": json.dumps({"msg": "已停止"}, ensure_ascii=False)}
            yield {"event": "done", "data": "{}"}

        # 历史回写：按 (用户, 会话) 隔离；缺 user_id 时不写，避免串号（停止时写已生成部分）
        if user_id and req.session_id and source in ("faq", "rag"):
            svc = SessionService()
            await svc.append_turn(user_id, req.session_id, "user", req.query)
            await svc.append_turn(user_id, req.session_id, "assistant", "".join(full_answer))

    return EventSourceResponse(event_gen())


@router.post("/chat/stop", response_model=ApiResponse[OkData], summary="停止当前会话的流式生成")
async def chat_stop(req: StopRequest, request: Request):
    """停止当前会话进行中的流式生成（幂等：无进行中生成也返回成功）。"""
    uid = _require_user(request)
    await set_chat_stop(uid, req.session_id)
    return success(OkData())


@router.delete("/chat/context/{session_id}", response_model=ApiResponse[OkData], summary="删除当前会话窗口记忆")
async def delete_context(session_id: str, request: Request):
    """删除当前会话的窗口记忆（压缩上下文），不影响对话历史与会话本身。"""
    uid = _require_user(request)
    await SessionService().clear_context(uid, session_id)
    return success(OkData())
