"""
loader_markdown - Markdown 加载器

Author: 骆昊
"""
import hashlib
import re
from pathlib import Path
from typing import Iterator

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from common.config import config
from common.logger import logger


def _strip_front_matter(text: str) -> str:
    pattern = r'^---\s*\n.*?\n---\s*\n'
    return re.sub(pattern, '', text, flags=re.DOTALL)


class FastMarkdownLoader(BaseLoader):
    """高性能纯文本 Markdown 加载器（保留原始标记语法）"""

    def __init__(self, file_path: str, remove_front_matter: bool = True):
        self.file_path = Path(file_path).resolve()
        self.remove_front_matter = remove_front_matter

    def lazy_load(self) -> Iterator[Document]:
        if not self.file_path.exists():
            logger.error(f'Markdown 文件加载失败，文件不存在: {self.file_path}')
            return

        if not self.file_path.is_file():
            logger.error(f'Markdown 文件加载失败，指定路径不是文件: {self.file_path}')
            return

        content = self._read_markdown_file()
        if content is None:
            return

        if self.remove_front_matter:
            content = _strip_front_matter(content)

        cleaned_text = content.strip()
        if not cleaned_text:
            logger.warning(f'Markdown 文件内容为空: {self.file_path}')
            return

        file_hash = hashlib.md5(cleaned_text.encode('utf-8')).hexdigest()[:16]

        yield Document(
            page_content=cleaned_text,
            metadata={
                'source': str(self.file_path),
                'file_name': self.file_path.name,
                'file_extension': '.md',
                'file_size': self.file_path.stat().st_size,
                'file_hash': file_hash,
            }
        )

    def _read_markdown_file(self) -> str | None:
        for enc in config.CANDIDATE_ENCODINGS:
            try:
                with open(self.file_path, encoding=enc) as file_obj:
                    return file_obj.read()
            except (UnicodeDecodeError, LookupError):
                continue
            except Exception as e:
                logger.error(f'读取 Markdown 文件失败 [{self.file_path}]: {e}', exc_info=True)
                return None

        try:
            with open(self.file_path, encoding='utf-8', errors='ignore') as file_obj:
                return file_obj.read()
        except Exception as e:
            logger.error(f'Markdown 强制读取失败 [{self.file_path}]: {e}', exc_info=True)
            return None


if __name__ == '__main__':
    md_loader = FastMarkdownLoader(str(config.FILES_DIR / 'Python学习资源汇总.md'))
    docs = md_loader.load()
    print(type(docs), len(docs))
    print(docs[0].page_content)
