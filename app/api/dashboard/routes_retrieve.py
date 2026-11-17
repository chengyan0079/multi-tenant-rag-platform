"""检索调试接口（运营后台，admin，SSE 流式）。

裸混合检索，返回原始召回片段（含分数/父块），供运营排查召回质量，不进入生成链路。
SSE 事件：meta（查询参数）/ chunk（单片段）/ done。
"""
import json

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.api.deps import resolve_tenant
from app.api.schemas import RetrieveRequest
from common.logger import logger
from core.rag_retriever.retriever import Retriever

router = APIRouter(prefix="/admin", tags=["retrieve-debug"])


@router.post("/retrieve", summary="检索调试（SSE 流式）")
async def retrieve(req: RetrieveRequest, tenant_id: int = Depends(resolve_tenant)):
    """运营侧裸检索调试：SSE 流式返回原始片段（含分数/父块），供排查召回。"""
    retriever = Retriever()

    async def event_gen():
        try:
            chunks = await retriever.retrieve(
                query=req.query, tenant_id=tenant_id,
                scope=req.scope, top_k=req.top_k, rerank=req.rerank,
            )
            yield {"event": "meta", "data": json.dumps(
                {"query": req.query, "scope": req.scope, "top_k": req.top_k, "total": len(chunks)},
                ensure_ascii=False)}
            for c in chunks:
                yield {"event": "chunk", "data": json.dumps(
                    {"chunk_id": c.chunk_id, "content": c.content, "doc_name": c.doc_name,
                     "page": c.page, "score": c.rerank_score, "parent_id": c.parent_id},
                    ensure_ascii=False)}
            yield {"event": "done", "data": "{}"}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"检索流式输出异常: {e}")
            yield {"event": "error", "data": json.dumps({"msg": "服务内部错误"}, ensure_ascii=False)}

    return EventSourceResponse(event_gen())
