"""
loader_pdf - PDF 文档加载器（文本 + 嵌入图片 OCR）

Author: 骆昊
"""
from io import BytesIO
from typing import Iterator
import cv2
import pymupdf
import numpy as np
from PIL import Image
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from tqdm import tqdm

from common.config import config
from common.logger import setup_logging, logger
from core.rag_indexer.document_loaders.ocr import get_ocr_instance

IMAGE_AREA_RATIO_THRESHOLD = 0.1
IMAGE_WIDTH_THRESHOLD, IMAGE_HEIGHT_THRESHOLD = 64, 64


def _rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
    h, w = image.shape[:2]
    rotate_center = (w / 2, h / 2)
    m = cv2.getRotationMatrix2D(rotate_center, angle, 1.0)
    new_w = int(h * np.abs(m[0, 1]) + w * np.abs(m[0, 0]))
    new_h = int(h * np.abs(m[0, 0]) + w * np.abs(m[1, 1]))
    m[0, 2] += (new_w - w) / 2
    m[1, 2] += (new_h - h) / 2
    return cv2.warpAffine(image, m, (new_w, new_h))


class OCRPDFLoader(BaseLoader):
    """PDF 文档加载器"""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path

    def lazy_load(self) -> Iterator[Document]:
        text_content = self.pdf2text()
        yield Document(page_content=text_content, metadata={'source': self.file_path})

    def pdf2text(self) -> str:
        pdf_doc = pymupdf.open(self.file_path)
        text_buffer = []

        ocr_obj = get_ocr_instance()
        try:
            pro_bar = tqdm(total=pdf_doc.page_count, desc='OCRPDFLoader processing')
            for page in pdf_doc:
                page_text = page.get_text('text')
                if page_text and page_text.strip():
                    text_buffer.append(page_text.strip())

                img_list = page.get_image_info(xrefs=True)
                for img in img_list:
                    xref = img.get('xref')
                    if not xref:
                        continue

                    bbox = img['bbox']
                    image_width, image_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    image_area = image_width * image_height
                    page_area = page.rect.width * page.rect.height
                    area_ratio = image_area / page_area

                    if (area_ratio < IMAGE_AREA_RATIO_THRESHOLD or
                            image_width < IMAGE_WIDTH_THRESHOLD or
                            image_height < IMAGE_HEIGHT_THRESHOLD):
                        continue

                    pix = pymupdf.Pixmap(pdf_doc, xref)
                    try:
                        if pix.n - pix.alpha < 4:
                            img_data = pix.tobytes('png')
                        else:
                            pix_rgb = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            img_data = pix_rgb.tobytes('png')
                            pix_rgb = None

                        image = Image.open(BytesIO(img_data)).convert('RGB')
                        img_array = np.array(image)

                        if int(page.rotation) != 0:
                            img_array = _rotate_image(img_array, 360 - page.rotation)

                        result, _ = ocr_obj(img_array)
                        if result:
                            ocr_result = [line[1] for line in result]
                            text_buffer.append('\n'.join(ocr_result))
                    finally:
                        pix = None

                pro_bar.update(1)
            pro_bar.close()
        finally:
            pdf_doc.close()

        return '\n\n'.join(text_buffer)


if __name__ == '__main__':
    setup_logging()
    pdf_loader = OCRPDFLoader(file_path=str(config.FILES_DIR / 'LLM基础知识.pdf'))
    docs = pdf_loader.load()
    logger.info(type(docs), len(docs))
    logger.info(docs[0].page_content)
