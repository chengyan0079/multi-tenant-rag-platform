"""
loader_factory - 文档加载器工厂

说明：创建型模式，按扩展名路由到适配的 Loader。

Author: 骆昊
"""
from pathlib import Path
from typing import Type

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from common.config import config
from common.logger import logger, setup_logging
from core.rag_indexer.document_loaders.loader_docx import OCRDocxLoader
from core.rag_indexer.document_loaders.loader_image import OCRImageLoader
from core.rag_indexer.document_loaders.loader_markdown import FastMarkdownLoader
from core.rag_indexer.document_loaders.loader_pdf import OCRPDFLoader
from core.rag_indexer.document_loaders.loader_pptx import OCRPPTLoader
from core.rag_indexer.document_loaders.loader_text import UTF8TextLoader


class DocumentLoaderFactory:
    """文档加载器工厂类，提供扩展名匹配、配置回退与批量加载功能"""

    SUPPORTED_LOADERS: dict[str, Type[BaseLoader]] = {
        '.txt':  UTF8TextLoader,
        '.pdf':  OCRPDFLoader,
        '.docx': OCRDocxLoader,
        '.pptx': OCRPPTLoader,
        '.jpg':  OCRImageLoader,
        '.jpeg': OCRImageLoader,
        '.png':  OCRImageLoader,
        '.bmp':  OCRImageLoader,
        '.md':   FastMarkdownLoader,
    }

    @classmethod
    def get_loader(cls, file_path: str, default_loader: BaseLoader | None = UTF8TextLoader) -> BaseLoader:
        """根据路径后缀匹配最佳 Loader"""
        path = Path(file_path)
        ext = path.suffix.lower()

        loader_cls = cls.SUPPORTED_LOADERS.get(ext, default_loader)

        if not loader_cls:
            raise ValueError(f'未找到适配文件 [{ext}] 的 Loader 且未配置默认的 Loader')

        logger.debug(f'文件 [{path.name}] 匹配到 Loader: {loader_cls.__name__}')
        return loader_cls(str(path))

    @classmethod
    def load_document(cls, file_path: str) -> list[Document]:
        """加载文档"""
        loader = cls.get_loader(file_path)
        return loader.load()

    @classmethod
    def is_supported(cls, file_path: str) -> bool:
        """判断指定文件格式是否在支持的加载列表内"""
        ext = Path(file_path).suffix.lower()
        return ext in cls.SUPPORTED_LOADERS


if __name__ == '__main__':
    setup_logging()
    thy_file = str(config.FILES_DIR / 'LLM基础知识.pdf')
    if DocumentLoaderFactory.is_supported(thy_file):
        docs = DocumentLoaderFactory.load_document(thy_file)
        logger.info(f'成功加载，文档页数/片段数: {len(docs)}')
        logger.info(docs[0].page_content)
