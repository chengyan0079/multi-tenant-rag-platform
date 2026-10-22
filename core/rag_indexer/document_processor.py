"""
document_processor - 文档处理模块（M2 索引链路）

流程：扫描目录 → 按类型加载 → 语义父块切分 → 递归子块切分 → 输出带父子关系的子块。
导入路径按本项目结构（core.rag_indexer）组织。
"""
import hashlib
import os
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from common.config import config
from common.logger import logger, setup_logging
from core.rag_indexer.document_loaders.loader_factory import DocumentLoaderFactory
from core.rag_indexer.semantic_splitter import BgeSemanticSplitter


def load_documents_from_directory(directory_path: str) -> list[Document]:
    """递归读取目录下所有支持的文件，并为每段补充文件路径与时间戳。"""
    documents: list[Document] = []

    for root, _, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)

            if not DocumentLoaderFactory.is_supported(file_path):
                logger.warning(f'不支持的文件类型，已跳过: {file_path}')
                continue

            try:
                loader = DocumentLoaderFactory.get_loader(file_path)
                loaded_docs = loader.load()

                current_time = datetime.now().isoformat()
                for doc in loaded_docs:
                    doc.metadata['file_path'] = file_path
                    doc.metadata['timestamp'] = current_time

                documents.extend(loaded_docs)
                logger.debug(f'加载文件 [{file}]: {len(loaded_docs)} 个片段')
            except Exception as e:
                logger.error(f'加载文件 {file_path} 失败: {e}', exc_info=True)

    return documents


def _generate_doc_hash(filepath: str, content: str) -> str:
    """用路径 + 正文前 200 字符生成稳定的短哈希，作为文档前缀 ID。"""
    hasher = hashlib.md5()
    hasher.update(filepath.encode('utf-8'))
    hasher.update(content[:200].encode('utf-8'))
    return hasher.hexdigest()[:12]


def process_documents(
        directory_path: str,
        parent_chunk_size: int = config.PARENT_CHUNK_SIZE,
        child_chunk_size: int = config.CHILD_CHUNK_SIZE,
        parent_chunk_overlap: int = config.PARENT_CHUNK_OVERLAP,
        child_chunk_overlap: int = config.CHILD_CHUNK_OVERLAP,
) -> list[Document]:
    """对文档做父子块切分，返回子块列表。

    父块用语义切分（BGE-M3），子块在父块内用递归字符切分；
    子块 metadata 保留 parent_id / parent_content，检索时可回退到上下文。
    """
    documents = load_documents_from_directory(directory_path)
    logger.debug(f'加载文档数量: {len(documents)}')

    if not documents:
        return []

    splitter_params = {
        'separators': [
            '\n\n', '\n', r'。|！|？', r'\.\s|\!\s|\?\s',
            r'；|;\s', r'，|,\s', ' ', ''
        ],
        'keep_separator': 'end',
        'is_separator_regex': True,
    }

    parent_splitter = BgeSemanticSplitter(
        model_path=str(config.MODELS_DIR / config.BGE_M3_DIR),
        breakpoint_threshold_amount=95
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=child_chunk_size,
        chunk_overlap=child_chunk_overlap,
        **splitter_params
    )

    all_chunks: list[Document] = []

    for i, doc in enumerate(documents, 1):
        file_path = doc.metadata.get('file_path', 'unknown_file_path')
        hash_prefix = _generate_doc_hash(file_path, doc.page_content)

        parent_chunks = parent_splitter.split_documents([doc])

        for j, parent_chunk in enumerate(parent_chunks):
            parent_id = f'doc_{hash_prefix}_parent_{j}'
            parent_chunk.metadata['parent_id'] = parent_id
            parent_chunk.metadata['parent_content'] = parent_chunk.page_content

            child_chunks = child_splitter.split_documents([parent_chunk])

            for k, sub_chunk in enumerate(child_chunks):
                sub_chunk.metadata['id'] = f'{parent_id}_child_{k}'
                all_chunks.append(sub_chunk)

    logger.debug(f'切分完成: 原始文档 {len(documents)} 篇 | 子块 {len(all_chunks)} 个')
    return all_chunks


if __name__ == '__main__':
    setup_logging()
    chunks = process_documents(config.UPLOAD_DIR)
    print(len(chunks))
