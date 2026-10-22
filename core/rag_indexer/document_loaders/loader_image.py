"""
loader_image - 图像（OCR）内容加载器

Author: 骆昊
"""
from pathlib import Path
from typing import Iterator, List

import numpy as np
from PIL import Image
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from common.config import config
from common.logger import logger, setup_logging
from core.rag_indexer.document_loaders.ocr import get_ocr_instance


class OCRImageLoader(BaseLoader):
    """图像 OCR 加载器（支持 JPG, PNG, BMP 等通用格式）"""

    def __init__(self, image_path: str):
        self.image_path = Path(image_path)

    def lazy_load(self) -> Iterator[Document]:
        text_content = self.image2text()
        yield Document(
            page_content=text_content,
            metadata={
                'source': str(self.image_path),
                'parse_status': 'success' if text_content else 'failed_or_empty'
            }
        )

    def image2text(self) -> str:
        if not self.image_path.exists():
            raise FileNotFoundError(f'图像文件不存在: {self.image_path}')

        ocr = get_ocr_instance()
        extracted_lines: List[str] = []

        try:
            with Image.open(self.image_path) as image:
                rgb_image = image.convert('RGB')
                img_array = np.array(rgb_image)

            result, _ = ocr(img_array)
            if result:
                extracted_lines = [line[1].strip() for line in result if line[1] and line[1].strip()]
        except Exception as e:
            logger.error(f'图像 OCR 解析失败 [{self.image_path.name}]: {e}', exc_info=True)
            return ''

        return '\n'.join(extracted_lines)


if __name__ == '__main__':
    setup_logging()
    img_loader = OCRImageLoader(str(config.FILES_DIR / 'RAG系统流程图.png'))
    doc = img_loader.load()
    logger.info(doc)
