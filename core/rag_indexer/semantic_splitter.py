"""
semantic_splitter - 基于 BGE-M3 的长文本语义切分器
"""
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_experimental.text_splitter import SemanticChunker
from langchain_huggingface import HuggingFaceEmbeddings

from common.config import config
from common.logger import logger, setup_logging


class BgeSemanticSplitter:
    """用 BGE-M3 向量做语义边界切分。"""

    def __init__(
        self,
        model_path: str | Path | None = None,
        device: str = 'cpu',
        breakpoint_threshold_type: str = 'percentile',
        breakpoint_threshold_amount: float = 80.0,
    ) -> None:
        self.model_path = model_path or str(config.MODELS_DIR / config.BGE_M3_DIR)
        logger.debug(f'加载 BGE-M3 向量模型用于语义切分: {self.model_path}')

        self.embeddings = HuggingFaceEmbeddings(
            model_name=self.model_path,
            model_kwargs={'device': device},
            encode_kwargs={'normalize_embeddings': True, 'batch_size': 16}
        )

        # 用向量距离找语义断点；句子级正则避免把长句切开
        self.chunker = SemanticChunker(
            embeddings=self.embeddings,
            breakpoint_threshold_type=breakpoint_threshold_type,
            breakpoint_threshold_amount=breakpoint_threshold_amount,
            sentence_split_regex=r'(?<=[。！？\n])'
        )
        logger.debug('BgeSemanticSplitter 初始化完成')

    def split_text(self, text: str) -> list[str]:
        """把一段文本切成若干语义块，返回块文本列表。"""
        if not text or not text.strip():
            return []
        try:
            documents: list[Document] = self.chunker.create_documents([text])
            chunks = [doc.page_content.strip() for doc in documents if doc.page_content.strip()]
            logger.debug(f'语义切分完成，共 {len(chunks)} 个块')
            return chunks
        except Exception as e:
            logger.error(f'语义切分异常: {e}', exc_info=True)
            return [text.strip()]

    def split_documents(self, documents: list[Document]) -> list[Document]:
        """对一批文档逐篇做语义切分；单篇失败则整篇作为一块返回。"""
        if not documents:
            return []

        result_docs: list[Document] = []
        for doc in documents:
            text = doc.page_content
            if not text or not text.strip():
                continue
            try:
                chunk_docs = self.chunker.create_documents([text])
                for chunk_doc in chunk_docs:
                    content = chunk_doc.page_content.strip()
                    if not content:
                        continue
                    result_docs.append(Document(page_content=content, metadata=doc.metadata.copy()))
            except Exception as e:
                logger.error(f'文档语义切分异常 [metadata: {doc.metadata}]: {e}', exc_info=True)
                result_docs.append(Document(page_content=text.strip(), metadata=doc.metadata.copy()))

        logger.debug(f'文档语义切分完成：输入 {len(documents)} 篇，输出 {len(result_docs)} 块')
        return result_docs


if __name__ == '__main__':
    splitter = BgeSemanticSplitter(device='cpu', breakpoint_threshold_type='percentile', breakpoint_threshold_amount=80)
    long_text = '中国对外贸易形势报告（75页）。前 10 个月，一般贸易进出口 19.5 万亿元，增长 25.1%。'
    results = splitter.split_text(long_text)
    for idx, chunk in enumerate(results, 1):
        print(f'===== Chunk {idx} | Length: {len(chunk)}) =====')
        print(chunk)
