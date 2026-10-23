"""
model_service - 模型服务

封装 bge-m3（一次推理同时产出 dense 与 sparse 向量）和 bge-reranker-v2-m3（重排）。
加载被拆为 load_embedding() / load_reranker() 两个独立步骤，便于 ModelManager 按
模型粒度注册预加载（见 app.managers.model_loaders）。load() 作为便捷组合保留。
由 AppServer 的 ModelManager 加载后注入 app_state.model_service。
"""
from typing import Any

# pymilvus 3.x 起把 model 子模块拆分为独立包 milvus-model；直接按 3.x 方式导入
from milvus_model.hybrid import BGEM3EmbeddingFunction
from milvus_model.reranker import BGERerankFunction

from common.config import config
from common.logger import logger, setup_logging


class ModelService:
    """向量与重排模型的加载与调用入口。"""

    def __init__(self, device: str = 'cpu') -> None:
        self.device = device
        self.embedding_function: BGEM3EmbeddingFunction | None = None
        self.reranker: BGERerankFunction | None = None
        self.dense_dim: int = 1024

    def load_embedding(self) -> None:
        """加载 bge-m3 向量模型到内存（幂等：已加载则跳过）。"""
        if self.embedding_function is not None:
            return
        bge_m3_path = str(config.MODELS_DIR / config.BGE_M3_DIR)
        logger.debug(f'加载 BGE-M3 向量模型: {bge_m3_path}')
        self.embedding_function = BGEM3EmbeddingFunction(
            model_name_or_path=bge_m3_path, use_f16=False, device=self.device
        )
        self.dense_dim = self.embedding_function.dim['dense']

    def load_reranker(self) -> None:
        """加载 bge-reranker 重排模型到内存（幂等：已加载则跳过）。"""
        if self.reranker is not None:
            return
        reranker_path = str(config.MODELS_DIR / config.BGE_RERANKER_DIR)
        logger.info(f'加载 BGE 重排模型: {reranker_path}')
        self.reranker = BGERerankFunction(reranker_path, device=self.device)
        logger.info('重排模型加载完成')

    def load(self) -> None:
        """便捷组合：依次加载两个模型。"""
        self.load_embedding()
        self.load_reranker()
        logger.info('模型加载完成')

    def embed(self, texts: list[str]) -> dict[str, Any]:
        """批量计算向量，返回 {'dense': ndarray, 'sparse': sparse_matrix}。"""
        if self.embedding_function is None:
            raise RuntimeError('Embedding 模型未加载')
        return self.embedding_function(texts)

    def rerank(self, query: str, documents: list[str], top_k: int = 8) -> list[Any]:
        """对候选文档做交叉编码重排，返回带 .index / .score 的结果列表。"""
        if self.reranker is None:
            raise RuntimeError('Reranker 模型未加载')
        return self.reranker(query=query, documents=documents, top_k=top_k)


if __name__ == '__main__':
    setup_logging()
    svc = ModelService()
    svc.load()
    emb = svc.embed(['你好世界'])
    print(emb['dense'][0][:4], emb['sparse'][0])
