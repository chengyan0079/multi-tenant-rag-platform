"""模型预加载加载器：每个模型实现 ModelLoader（load/unload 写回 app_state），ModelManager 持注册表遍历预加载，新增模型零改 Manager。同步加载用 asyncio.to_thread 包裹。"""
import asyncio
from abc import ABC, abstractmethod
from typing import Any

from app.app_state import app_state
from common.logger import logger


class ModelLoader(ABC):
    """模型预加载抽象：name 标识，load 执行加载并注入 app_state，unload 可选释放。"""

    name: str = 'unnamed'

    @abstractmethod
    async def load(self) -> None: ...

    async def unload(self) -> None:
        """释放资源（可选），默认空实现。"""


def _ensure_service() -> Any:
    """保证 app_state.model_service 容器存在（嵌入与重排共享同一 ModelService 实例）。

    延迟导入 ModelService，避免其依赖（pymilvus 等）在模块加载期就传染到 Manager。
    """
    if app_state.model_service is None:
        from core.rag_model.model_service import ModelService
        app_state.model_service = ModelService()
    return app_state.model_service


class EmbeddingModelLoader(ModelLoader):
    """BGE-M3 向量模型预加载，注入 app_state.model_service.embedding_function。"""

    name = 'bge-m3(embedding)'

    async def load(self) -> None:
        svc = _ensure_service()
        await asyncio.to_thread(svc.load_embedding)

    async def unload(self) -> None:
        if app_state.model_service is not None:
            app_state.model_service.embedding_function = None


class RerankerModelLoader(ModelLoader):
    """BGE-Reranker 预加载，注入 app_state.model_service.reranker。"""

    name = 'bge-reranker'

    async def load(self) -> None:
        svc = _ensure_service()
        await asyncio.to_thread(svc.load_reranker)

    async def unload(self) -> None:
        if app_state.model_service is not None:
            app_state.model_service.reranker = None


class IntentClassifierLoader(ModelLoader):
    """意图分类 BERT 模型预加载：触发 core.intent 单例预热并写入 app_state。

    模型缺失（未训练/依赖未装）时 get_classifier 返回 None，已降级，不阻断启动。
    """

    name = 'intent-bert'

    async def load(self) -> None:
        from core.intent import get_classifier
        clf = await asyncio.to_thread(get_classifier)
        app_state.intent_classifier = clf
        if clf is None:
            logger.warning('意图分类模型未加载（缺依赖或未训练），分类将降级为默认标签')

    async def unload(self) -> None:
        app_state.intent_classifier = None
