"""API 请求体模型（接入层 M1）——与响应模型（response.py）分离维护。

用 Pydantic 做字段校验（类型 / 必填 / 取值约束），非法输入由全局校验处理器
统一返回 422 + {code: PARAM_ERROR}。只依赖 pydantic，不 import ORM / core / common。

约定：
- 必填 = 无默认值（Field(...)）；可选 = 有默认值 / None。description 以【必填】/【可选】前缀标明。
- 必填字符串统一用 NonBlank：拒绝空串与纯空白字符（保留原值，不做 trim）。
"""
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field


def _non_blank(v: str) -> str:
    """必填字符串校验：拒绝空串与纯空白（保留原值，不做 trim）。"""
    if not v or not v.strip():
        raise ValueError('不允许为空或纯空白字符')
    return v


# 必填字符串类型：在 str 之上追加“非空 / 非纯空白”校验
NonBlank = Annotated[str, AfterValidator(_non_blank)]


class ChatRequest(BaseModel):
    query: NonBlank = Field(..., max_length=2000, description="【必填】用户问题")
    session_id: str | None = Field(None, max_length=64, description="【可选】会话 id（由 /v1/sessions 创建，随请求体传入）")


class StopRequest(BaseModel):
    session_id: NonBlank = Field(..., max_length=64, description="【必填】要停止生成的会话 id（由 /v1/sessions 创建）")


class TurnRequest(BaseModel):
    role: Literal["user", "assistant"] = Field(..., description="【必填】发言角色：user / assistant")
    content: NonBlank = Field(..., max_length=8000, description="【必填】该轮文本内容")


class SessionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=100, description="【可选】用户编辑的会话标题")


class FaqRequest(BaseModel):
    question: NonBlank = Field(..., max_length=512, description="【必填】标准问题")
    answer: NonBlank = Field(..., description="【必填】标准答案")
    question_type: Literal[0, 1] = Field(0, description="【可选】问题类型：0=通用知识 1=专业咨询（对齐 QuestionType）")
    similar_questions: str | None = Field(None, description="【可选】相似问题（多行文本，命中候选）")


class DocRequest(BaseModel):
    file_id: int = Field(..., description="【必填】上传后得到的文档ID（rag_file.id）")
    doc_type: Literal[0, 1] = Field(0, description="【可选】文档类型：0=普通文档 1=FAQ 来源")


class RetrieveRequest(BaseModel):
    query: NonBlank = Field(..., max_length=2000, description="【必填】检索问题")
    scope: Literal["doc", "faq"] = Field("doc", description="【可选】检索域：doc=文档 faq=问答对")
    top_k: int = Field(8, ge=1, le=50, description="【可选】召回片段数上限")
    rerank: bool = Field(True, description="【可选】是否启用重排")


class CreateRequestIn(BaseModel):
    namespace: NonBlank = Field(..., description="【必填】Nacos 命名空间")
    group_id: NonBlank = Field(..., description="【必填】配置分组")
    data_id: NonBlank = Field(..., max_length=256, description="【必填】配置 data_id")
    domain: NonBlank = Field(..., max_length=32, description="【必填】业务域（prompt/config/...）")
    op_type: Literal[1, 2, 3] = Field(..., description="【必填】操作类型：1=新增 2=修改 3=删除")
    content_new: str | None = Field(..., description="【必填】新内容（新增/修改必填；删除传 null）")
    content_format: Literal["json", "text", "yaml"] = Field(..., description="【必填】内容格式")
    title: NonBlank = Field(..., max_length=256, description="【必填】申请标题")
    reason: NonBlank = Field(..., max_length=512, description="【必填】变更理由")


class ReviewIn(BaseModel):
    action: Literal[1, 2] = Field(..., description="【必填】复核动作：1=通过 2=驳回")
    comment: str | None = Field(None, max_length=512, description="【可选】复核意见")


class RollbackIn(BaseModel):
    namespace: NonBlank = Field(..., description="【必填】Nacos 命名空间")
    group_id: NonBlank = Field(..., description="【必填】配置分组")
    data_id: NonBlank = Field(..., max_length=256, description="【必填】配置 data_id")
    domain: NonBlank = Field(..., max_length=32, description="【必填】业务域")
    src_history_nid: int = Field(..., ge=1, description="【必填】回滚目标历史版本 nid")
    title: NonBlank = Field(..., max_length=256, description="【必填】回滚标题")
    reason: NonBlank = Field(..., max_length=512, description="【必填】回滚理由")


__all__ = [
    "ChatRequest", "StopRequest", "TurnRequest", "SessionPatch", "FaqRequest", "DocRequest",
    "RetrieveRequest", "CreateRequestIn", "ReviewIn", "RollbackIn",
]
