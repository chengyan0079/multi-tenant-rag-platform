"""ModelManager（order=50）：按注册表预加载模型并注入 app_state；默认 embedding/reranker/intent-bert 三加载器；单模型失败不影响启动（如意图分类缺失降级）。"""
from typing import List

from app.server import Manager
from app.app_state import app_state
from common.logger import logger
from app.managers.model_loaders import (
    ModelLoader, EmbeddingModelLoader, RerankerModelLoader, IntentClassifierLoader,
)


class ModelManager(Manager):
    def __init__(self) -> None:
        # 默认注册核心模型加载器；如需新增，调用 register_loader 即可
        self.loaders: List[ModelLoader] = [
            EmbeddingModelLoader(),
            RerankerModelLoader(),
            IntentClassifierLoader(),
        ]

    def register_loader(self, loader: ModelLoader) -> None:
        """注册额外模型预加载（解耦：Manager 不感知具体模型类型）。"""
        self.loaders.append(loader)

    async def start(self) -> None:
        loaded: List[str] = []
        for loader in self.loaders:
            try:
                logger.info(f'预加载模型: {loader.name}')
                await loader.load()
                loaded.append(loader.name)
            except Exception as e:
                # 单个模型失败不阻断其他模型与服务启动（如意图分类缺失即降级）
                logger.warning(f'模型预加载失败（已降级，不影响启动）: {loader.name}: {e}')
        if app_state.model_service is None:
            # 保证容器存在，避免后续 embed/rerank 调用报 None
            from core.rag_model.model_service import ModelService
            app_state.model_service = ModelService()
        logger.info(f'模型预加载完成: {loaded}')

    async def stop(self) -> None:
        for loader in self.loaders:
            try:
                await loader.unload()
            except Exception as e:
                logger.warning(f'模型释放失败: {loader.name}: {e}')
