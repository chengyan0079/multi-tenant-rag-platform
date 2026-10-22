"""
ocr - 光学文字识别模块

说明：使用 functools.lru_cache 实现单例模式，避免内存中重复加载模型权重造成 OOM。

Author: 骆昊
"""
from functools import lru_cache

from common.logger import logger


@lru_cache(maxsize=1)
def get_ocr_instance(use_cuda: bool = True):
    """全局单例获取 RapidOCR，避免多次加载权重模型导致内存/显存溢出"""
    try:
        from rapidocr_paddle import RapidOCR
        logger.debug('成功加载 rapidocr_paddle (GPU/CPU)')
        return RapidOCR(det_use_cuda=use_cuda, cls_use_cuda=use_cuda, rec_use_cuda=use_cuda)
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR
        logger.debug('降级加载 rapidocr_onnxruntime (CPU)')
        return RapidOCR()
