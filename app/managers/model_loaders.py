"""
model_loaders - 模型预加载加载器（与具体加载解耦）

设计：每个具体模型实现一个 ModelLoader（name + load/unload），负责自身加载并写回
app_state。ModelManager 只持有 loaders 注册表、遍历预加载，不感知具体模型类型。
新增模型只需新增一个 Loader，并向 ModelManager.register_loader 注册，Manager 无需改动。

加载细节（同步 CPU/GPU 加载）统一用 asyncio.to_thread 包裹，避免阻塞事件循环。
"""
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
    """保证 app_state.model 容器存在（嵌入与重排共享同一 ModelService 实例）。

    延迟导入 ModelService，避免其依赖（pymilvus 等）在模块加载期就传染到 Manager。
    """
    if app_state.model is None:
        from core.rag_model.model_service import ModelService
        app_state.model = ModelService()
    return app_state.model


class EmbeddingModelLoader(ModelLoader):
    """BGE-M3 向量模型预加载，注入 app_state.model.embedding_function。"""

    name = 'bge-m3(embedding)'

    async def load(self) -> None:
        svc = _ensure_service()
        await asyncio.to_thread(svc.load_embedding)

    async def unload(self) -> None:
        if app_state.model is not None:
            app_state.model.embedding_function = None


class RerankerModelLoader(ModelLoader):
    """BGE-Reranker 预加载，注入 app_state.model.reranker。"""

    name = 'bge-reranker'

    async def load(self) -> None:
        svc = _ensure_service()
        await asyncio.to_thread(svc.load_reranker)

    async def unload(self) -> None:
        if app_state.model is not None:
            app_state.model.reranker = None


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
