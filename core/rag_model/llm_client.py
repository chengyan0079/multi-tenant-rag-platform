"""
llm_client - 共享的大模型调用助手（异步）

封装 OpenAI 兼容网关（平台 LLM 网关）的纯文本补全调用，供检索策略路由、
query 改写等需要"文本进、文本出"的场景复用，避免在各处重复构造 ChatOpenAI。
"""
from common.config import config
from common.logger import logger
from langchain_openai import ChatOpenAI


def build_llm(*, temperature: float = 0.0, max_tokens: int = 512, streaming: bool = False) -> ChatOpenAI:
    """构造一个 OpenAI 兼容的 ChatOpenAI 客户端（按平台配置）。"""
    return ChatOpenAI(
        model=config.LLM_MODEL_NAME,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
    )


async def acomplete(prompt: str, *, temperature: float = 0.0, max_tokens: int = 512) -> str:
    """异步纯文本补全：把 prompt 作为单条 human 消息送入，返回文本。

    失败返回空串并记 warning（调用方据此降级）。
    """
    try:
        llm = build_llm(temperature=temperature, max_tokens=max_tokens)
        msg = await llm.ainvoke(prompt)
        return msg.content if isinstance(msg.content, str) else ''
    except Exception as e:  # noqa: BLE001
        logger.warning(f'LLM 补全调用失败: {e}')
        return ''
