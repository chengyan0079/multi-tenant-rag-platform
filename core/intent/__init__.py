"""查询意图识别（在线推理，二分类：通用知识/专业咨询）。QueryClassifier 同步推理（批量+缓存+设备自适应），get_classifier() 进程级单例懒加载、失败降级 None；classify_* 异步入口用 asyncio.to_thread 包裹 BERT 前向避免阻塞。训练评估在 offline/train_intent.py；torch 缺失时仍可导入，分类恒默认标签不影响主链路。"""
import asyncio
import threading
from typing import Optional

from common.config import config
from common.logger import logger

try:
    from .classifier import QueryClassifier
    _has_deps = True
except Exception as e:  # 缺 torch 等重依赖时整体降级
    logger.warning(f'意图分类依赖(torch)缺失，分类将始终降级为默认标签: {e}')
    QueryClassifier = None  # type: ignore
    _has_deps = False

__all__ = ['QueryClassifier', 'get_classifier', 'classify_queries', 'classify_query']

# 单例：加载成功存实例；加载失败存哨兵，避免每个请求重复尝试 from_pretrained。
_classifier: 'QueryClassifier | None | object' = None
_classifier_sentinel = object()
_lock = threading.Lock()


def get_classifier() -> Optional[QueryClassifier]:
    """进程级单例：首次调用加载模型，后续复用；依赖缺失或模型缺失时返回 None。

    训练产出模型后需重启进程才能生效（单例在进程生命周期内缓存）。
    """
    global _classifier
    if not _has_deps:
        return None
    if _classifier is not None:
        return _classifier if _classifier is not _classifier_sentinel else None
    with _lock:
        if _classifier is None:
            try:
                _classifier = QueryClassifier(str(config.MODELS_DIR / config.BERT_INTENT_DIR))  # type: ignore[assignment]
            except Exception as e:
                logger.warning(f'意图分类模型加载失败，分类将降级为默认标签: {e}')
                _classifier = _classifier_sentinel
    return _classifier if _classifier is not _classifier_sentinel else None


async def classify_queries(queries: list[str]) -> list[str]:
    """批量异步分类；结果按输入顺序返回。模型/依赖不可用时全部返回默认标签。"""
    clf = get_classifier()
    if clf is None:
        return ['通用知识'] * len(queries)
    # BERT 前向是 CPU 密集，包 to_thread 释放事件循环，不阻塞其他请求
    return await asyncio.to_thread(clf.predict_batch, queries)


async def classify_query(query: str) -> str:
    """单条异步分类。"""
    return (await classify_queries([query]))[0]
