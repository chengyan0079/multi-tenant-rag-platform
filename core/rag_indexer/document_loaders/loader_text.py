"""
loader_text - 纯文本加载器

Author: 骆昊
"""
import hashlib
from pathlib import Path
from typing import Iterator

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from common.config import config
from common.logger import logger, setup_logging


class UTF8TextLoader(BaseLoader):
    """纯文本加载器"""

    def __init__(
        self,
        file_path: str,
        encoding: str | None = 'utf-8',
        autodetect_encoding: bool = True
    ):
        self.file_path = Path(file_path).resolve()
        self.encoding = encoding
        self.autodetect_encoding = autodetect_encoding

    def lazy_load(self) -> Iterator[Document]:
        if not self.file_path.exists():
            logger.error(f'TextLoader 加载失败，文件不存在: {self.file_path}')
            return

        if not self.file_path.is_file():
            logger.error(f'TextLoader 加载失败，指定路径不是文件: {self.file_path}')
            return

        content = self._read_file_with_fallback()
        if content is None:
            logger.error(f'无法正确读取文件内容（编码无法识别）: {self.file_path}')
            return

        cleaned_text = content.strip()
        file_hash = hashlib.md5(cleaned_text.encode("utf-8")).hexdigest()[:16]

        yield Document(
            page_content=cleaned_text,
            metadata={
                'source': str(self.file_path),
                'file_name': self.file_path.name,
                'file_extension': self.file_path.suffix.lower(),
                'file_size': self.file_path.stat().st_size,
                'file_hash': file_hash,
            }
        )

    def _read_file_with_fallback(self) -> str | None:
        encodings_to_try: list[str] = []
        if self.encoding:
            encodings_to_try.append(self.encoding)
        if self.autodetect_encoding:
            for fallback_enc in config.CANDIDATE_ENCODINGS:
                if fallback_enc not in encodings_to_try:
                    encodings_to_try.append(fallback_enc)

        for enc in encodings_to_try:
            try:
                with open(self.file_path, encoding=enc) as file_obj:
                    return file_obj.read()
            except (UnicodeDecodeError, LookupError):
                continue
            except Exception as e:
                logger.error(f'读取文件 {self.file_path} 时发生未知 I/O 错误: {e}', exc_info=True)
                return None

        try:
            with open(self.file_path, encoding='utf-8', errors='ignore') as file_obj:
                logger.warning(f'文件 {self.file_path.name} 使用标准编码失败，已触发 utf-8 强制忽略模式读取')
                return file_obj.read()
        except Exception as e:
            logger.error(f'终极读取兜底失败 [{self.file_path.name}]: {e}', exc_info=True)
            return None


if __name__ == '__main__':
    setup_logging()
    text_loader = UTF8TextLoader(str(config.FILES_DIR / '数据库和SQL完整笔记.txt'))
    docs = text_loader.load()
    logger.info(type(docs), len(docs))
    logger.info(docs[0].page_content)
