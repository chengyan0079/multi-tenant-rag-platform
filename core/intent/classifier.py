"""
intent/classifier - 查询意图分类推理器（高性能版）

基于 BERT 的二分类（通用知识 / 专业咨询）。为降低在线推理开销，做了以下性能优化：
1. 模型全局单例，仅加载一次（避免重复 from_pretrained 的开销）。
2. 支持批量推理 predict_batch：一次 tokenize + 一次前向，显著优于逐条调用。
3. 结果内存缓存：相同 query 直接命中，跳过模型前向。
4. 推理用 torch.inference_mode() 关闭梯度与额外开销。
5. 自动选择 cuda / mps / cpu 设备；GPU 下推理更快。
"""
import threading

import torch
from transformers import BertTokenizer, BertForSequenceClassification

from common.config import config
from common.logger import logger


class QueryClassifier:
    """轻量级查询意图分类推理器（二分类）。"""

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.device = torch.device(
            'cuda' if torch.cuda.is_available()
            else 'mps' if torch.mps.is_available()
            else 'cpu'
        )
        self.max_length = 128
        self.default_label = '通用知识'
        self.idx_to_label = {0: '通用知识', 1: '专业咨询'}

        logger.debug(f'加载意图分类模型: {model_path}, 设备: {self.device}')
        self.tokenizer = BertTokenizer.from_pretrained(model_path)
        self.model = BertForSequenceClassification.from_pretrained(model_path)
        self.model.to(self.device)
        self.model.eval()

        self._cache: dict[str, str] = {}
        self._cache_max = 4096
        self._lock = threading.Lock()

    def _encode(self, texts: list[str]):
        # 批量 tokenize：padding=True 自动对齐到批次内最长，避免固定长 padding 的算力浪费
        return self.tokenizer(
            texts, truncation=True, padding=True,
            max_length=self.max_length, return_tensors='pt',
        )

    def predict_batch(self, queries: list[str]) -> list[str]:
        """批量意图分类：一次前向完成整批，性能远好于逐条调用。"""
        results: list[str | None] = [None] * len(queries)
        pending_idx: list[int] = []
        pending_texts: list[str] = []

        # 1) 空串与缓存命中先行处理，减少不必要的模型计算
        for i, q in enumerate(queries):
            q = (q or '').strip()
            if not q:
                results[i] = self.default_label
                continue
            cached = self._cache.get(q)
            if cached is not None:
                results[i] = cached
            else:
                pending_idx.append(i)
                pending_texts.append(q)

        if not pending_texts:
            return [r for r in results if r is not None]

        # 2) 批量推理
        inputs = self._encode(pending_texts)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.inference_mode():
            logits = self.model(**inputs).logits
            preds = torch.argmax(logits, dim=1).tolist()

        # 3) 回填结果并写缓存（加锁保证缓存一致性，避免并发重复推理）
        with self._lock:
            for offset, orig_i in enumerate(pending_idx):
                label = self.idx_to_label.get(preds[offset], self.default_label)
                results[orig_i] = label
                self._cache[pending_texts[offset]] = label
            if len(self._cache) > self._cache_max:
                self._cache.clear()

        return [r for r in results if r is not None]

    def predict(self, query: str) -> str:
        """单条分类，内部复用批量接口。"""
        return self.predict_batch([query])[0]
