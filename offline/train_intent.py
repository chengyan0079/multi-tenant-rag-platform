"""
train_intent - 离线意图分类模型训练与评估（不属在线运行时）

数据格式：JSONL，每行 {"query": str, "label": "通用知识"|"专业咨询"}。
产出：微调后的 BERT 分类模型，落到 config.BERT_INTENT_DIR，供 core.intent 推理加载。
本脚本是离线流程，项目在线服务不参与训练/评估，仅消费其产出。

运行：python offline/train_intent.py
"""
import json
import os

import numpy as np
import torch
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from torch.utils.data import Dataset
from transformers import (
    BertTokenizer, BertForSequenceClassification, Trainer, TrainingArguments,
)

from common.config import config
from common.logger import logger, setup_logging


class IntentDataset(Dataset):
    """自定义数据集：把 tokenize 后的 inputs 与标签打包。"""

    def __init__(self, inputs, labels):
        self.inputs = inputs
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        item = {key: val[idx] for key, val in self.inputs.items()}
        item['labels'] = torch.tensor(self.labels[idx], dtype=torch.int64)
        return item


def _evaluate(model, inputs, labels, idx_to_label) -> None:
    """用验证集评估并输出混淆矩阵与分类报告。"""
    dataset = IntentDataset(inputs, labels)
    trainer = Trainer(model=model)
    predictions = trainer.predict(dataset)
    pred_labels = np.argmax(predictions.predictions, axis=-1)
    label_names = [idx_to_label[i] for i in sorted(idx_to_label)]
    logger.info(f'混淆矩阵:\n{confusion_matrix(labels, pred_labels)}')
    logger.info('分类报告:\n' + classification_report(
        labels, pred_labels, target_names=label_names, zero_division=0,
    ))


class QueryClassifierTrainer:
    """离线模型训练器：底座 BERT + 业务数据微调，产出二分类意图模型。"""

    def __init__(self, base_model_dir: str, model_output_dir: str) -> None:
        self.base_model_dir = base_model_dir
        self.model_output_dir = model_output_dir
        self.tokenizer = BertTokenizer.from_pretrained(base_model_dir)
        self.label_to_idx = {'通用知识': 0, '专业咨询': 1}
        self.idx_to_label = {v: k for k, v in self.label_to_idx.items()}

    def _load_data(self, data_file: str):
        if not os.path.exists(data_file):
            raise FileNotFoundError(f'数据集文件不存在: {data_file}')
        with open(data_file, encoding='utf-8') as f:
            data = [json.loads(line) for line in f if line.strip()]
        texts = [item['query'] for item in data]
        labels = [self.label_to_idx[item['label']] for item in data]
        return texts, labels

    def train(
        self, data_file: str,
        epochs: int = 4, batch_size: int = 16, max_length: int = 128,
    ) -> None:
        """执行训练与评估主流程。"""
        texts, labels = self._load_data(data_file)
        train_texts, valid_texts, train_labels, valid_labels = train_test_split(
            texts, labels, test_size=0.2, stratify=labels, random_state=42,
        )
        params = {'truncation': True, 'padding': True, 'max_length': max_length, 'return_tensors': 'pt'}
        train_inputs = self.tokenizer(train_texts, **params)
        valid_inputs = self.tokenizer(valid_texts, **params)
        train_dataset = IntentDataset(train_inputs, train_labels)
        valid_dataset = IntentDataset(valid_inputs, valid_labels)

        model = BertForSequenceClassification.from_pretrained(self.base_model_dir, num_labels=2)
        training_args = TrainingArguments(
            output_dir=str(config.TMP_DIR / 'bert_ft_results'),
            num_train_epochs=epochs,
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            warmup_steps=32,
            weight_decay=0.01,
            logging_steps=8,
            eval_strategy='epoch',
            save_strategy='epoch',
            load_best_model_at_end=True,
            save_total_limit=1,
            metric_for_best_model='eval_loss',
            # 仅在 GPU 上开启半精度，CPU/mps 下无关或可能报错
            fp16=False,
            dataloader_pin_memory=False,
        )
        trainer = Trainer(
            model=model, args=training_args,
            train_dataset=train_dataset, eval_dataset=valid_dataset,
            compute_metrics=lambda p: {
                'accuracy': (np.argmax(p.predictions, axis=-1) == p.label_ids).mean()
            },
        )
        logger.debug('开始训练意图分类模型...')
        trainer.train()

        os.makedirs(self.model_output_dir, exist_ok=True)
        model.save_pretrained(self.model_output_dir)
        self.tokenizer.save_pretrained(self.model_output_dir)
        logger.debug(f'模型已导出至: {self.model_output_dir}')

        _evaluate(model, valid_inputs, valid_labels, self.idx_to_label)


if __name__ == '__main__':
    setup_logging()
    trainer = QueryClassifierTrainer(
        base_model_dir=str(config.MODELS_DIR / config.BERT_MODEL_DIR),
        model_output_dir=str(config.MODELS_DIR /config.BERT_INTENT_DIR),
    )
    trainer.train(str(config.TRAIN_DIR / 'model_generic_1830.json'))
