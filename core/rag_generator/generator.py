"""
generator - 生成层（M8）

把检索结果拼成上下文，交给 LangChain 组装成调用大模型的链。
大模型走平台 LLM 网关（OpenAI 兼容接口）。支持多轮历史拼接。
"""
import asyncio
from typing import Any

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableParallel
from langchain_openai import ChatOpenAI

from common.config import config
from common.logger import logger
from core.config_center.prompts_manager import RAGPromptsManager
from core.rag_retriever.retriever import RetrievedChunk
from store.session_store import SessionStore
from core.session_memory.memory_service import MemoryService

# 提示词本体已迁移到 Nacos 配置中心 PROMPT 组（data_id=CORE_RAG_TEMPLATE 等），
# 运行时由 RAGPromptsManager 按 "先 Redis 再 Nacos" 读取。下方为 Nacos 不可达时的兜底模板，
# 避免生成链路整体不可用。
# 注意：通用知识走"空上下文直答"，故模板需允许"上下文为空/不足时用自身知识回答"；
# Nacos 中的 CORE_RAG_TEMPLATE 应保持同一语义（勿写成"无上下文即拒绝回答"）。
_FALLBACK_TEMPLATE = (
    "你是智能体助手。请依据下面的上下文回答用户问题：上下文能回答则依据它并标注来源；"
    "上下文为空或不足以回答时，请用你自身的知识直接回答，不要编造来源。\n"
    "上下文里用 <检索定位区块> ... </检索定位区块> 包裹的片段，"
    "是检索系统命中的核心段落；请在作答时把注意力集中到这些片段上，优先依据它们组织答案。\n"
    "{context}\n\n用户问题：{question}"
)


def to_documents(chunks: list[RetrievedChunk]) -> list[Document]:
    """把检索片段转成 LangChain 文档，附带来源等元数据。"""
    return [Document(page_content=c.content, metadata={
        'doc_name': c.doc_name, 'page': c.page, 'oss_key': c.oss_key, 'score': c.rerank_score
    }) for c in chunks]


def format_docs(docs: list[Document]) -> str:
    """把文档列表拼成带来源的上下文文本。"""
    return "\n\n".join(
        f"[来源:{d.metadata.get('doc_name', '未知')} P{d.metadata.get('page', 0)}]\n{d.page_content}"
        for d in docs
    )


class Generator:
    """检索结果 → Prompt → 大模型 的生成封装。"""

    def __init__(self) -> None:
        self.conv_store = SessionStore()
        self.memory_service = MemoryService(session_store=self.conv_store)

    async def _answer_prompt(self) -> Any:
        """取直接回答提示词（CORE_RAG_TEMPLATE）；Nacos 不可达时回退内置兜底模板。

        提示词读取是同步的 Redis/Nacos 调用，用 to_thread 避免阻塞事件循环。
        """
        try:
            return await asyncio.to_thread(RAGPromptsManager.core_rag_prompt)
        except Exception:  # noqa: BLE001
            logger.warning('直接回答提示词读取失败，使用内置兜底模板')
            from langchain_core.prompts import PromptTemplate
            return PromptTemplate.from_template(_FALLBACK_TEMPLATE)

    def _build_chain(self, prompt_template: Any) -> Any:
        """构造 LCEL 链：并行准备上下文与问题 → Prompt → 大模型 → 文本解析。"""
        llm = ChatOpenAI(
            model=config.LLM_MODEL_NAME,
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
            streaming=True,
        )
        return (
            RunnableParallel({
                "context": lambda x: format_docs(to_documents(x["chunks"])),
                "question": lambda x: x["question"],
            })
            | prompt_template
            | llm | StrOutputParser()
        )

    async def generate(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        history: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """生成答案。history 为空且给了 session_id + user_id 时，按用户隔离取历史与记忆。"""
        if history is None and session_id and user_id:
            history = await self.conv_store.get_history(user_id, session_id)
            memory = await self.memory_service.get(user_id, session_id)
            memory_text = MemoryService.build_injection_text(memory)
        else:
            memory_text = ''

        # 多轮：记忆前置提供长期上下文；历史对话拼到问题前面，缓解指代歧义
        question = query
        if memory_text:
            question = f"{memory_text}\n\n{question}"
        if history:
            ctx = "\n".join(f"{h['role']}: {h['content']}" for h in history)
            question = f"历史对话:\n{ctx}\n\n当前问题: {question}"

        prompt_template = await self._answer_prompt()
        chain = self._build_chain(prompt_template)
        try:
            return await chain.ainvoke({"question": question, "chunks": chunks})
        except Exception as e:
            logger.error(f'生成失败: {e}', exc_info=True)
            return '抱歉，生成答案时发生错误。'

    async def astream(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        history: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ):
        """流式生成：逐 token yield 文本（供 /qa SSE 输出）。

        历史 / 记忆拼接逻辑与 generate 一致；底层链已 streaming=True，
        通过 chain.astream 增量产出，失败时 yield 兜底文案。
        """
        if history is None and session_id and user_id:
            history = await self.conv_store.get_history(user_id, session_id)
            memory = await self.memory_service.get(user_id, session_id)
            memory_text = MemoryService.build_injection_text(memory)
        else:
            memory_text = ''

        question = query
        if memory_text:
            question = f"{memory_text}\n\n{question}"
        if history:
            ctx = "\n".join(f"{h['role']}: {h['content']}" for h in history)
            question = f"历史对话:\n{ctx}\n\n当前问题: {question}"

        prompt_template = await self._answer_prompt()
        chain = self._build_chain(prompt_template)
        try:
            async for piece in chain.astream({"question": question, "chunks": chunks}):
                if piece:
                    yield piece
        except Exception as e:
            logger.error(f'流式生成失败: {e}', exc_info=True)
            yield '抱歉，生成答案时发生错误。'
