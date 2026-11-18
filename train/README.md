# train/ · 故障要素抽取数据集

用于 LLaMA-Factory 对 `Qwen2.5-1.5B-Instruct` 做 LoRA 指令微调，从故障复盘正文抽取结构化要素（fault_id / title / severity / start_time / symptoms / root_cause / change / runbook），供图谱语义路线入库。

## 数据构成

| 文件 | 条数 | 说明 |
|---|---|---|
| `data/fault_extract.json` | 200 | 20 类故障场景 × 10 条变体。变体只替换故障单号、日期与部分度量数值，场景语义相同 |
| `data/fault_eval.json` | 20 | 20 类场景各 1 条变体（旧切分，见下） |
| `schema/fault_extract_schema.json` | - | 输出 JSON Schema，字段缺失一律填 null，不允许推测 |

数据为合成构造：以 20 类真实故障模式为骨架，按模板生成复盘正文与标注，覆盖 P1/P2/P3 三档。**不是线上原始复盘**，因此指标只能解释为「模板内抽取能力」，不能外推为真实复盘的泛化能力。

## 切分问题与修订

原切分按 `fault_id` 划分，`fault_eval.json` 与训练集单号不重叠。但同场景 10 条变体症状名完全相同，归一化单号与时间后正文几乎一致——**留出集与训练集共享全部 20 个场景**，在该留出集上得到的指标反映记忆而非泛化。

可用 `audit_leakage.py` 复现该结论：

```bash
python train/audit_leakage.py
# 场景交叠：留出集 20 个场景，其中 20 个在训练集出现过
# 标签泄漏：20 条的症状名集合与其同场景训练样本完全一致
```

修订后的切分为场景级留出，留出场景在训练集中一条都不出现：

```bash
python train/split_holdout.py --holdout 5 --per-scenario 4 --seed 42
# 训练 150 条（15 类场景） / 留出 20 条（5 类场景） / 场景交叠 0
```

产出在 `data/holdout/`（`train.json`、`eval.json`、`dataset_info.json`）。`dataset_info.json` 可直接合并进 LLaMA-Factory 的 `data/dataset_info.json`，数据集名 `fault_extract_holdout` 与 `fault_extract_holdout_eval`。

留出集数量偏少（20 条、5 类场景），指标方差大。需要更稳的数字用 `--per-scenario 0`，留出场景的全部 10 条变体，得 50 条。

## 训练参数

`llamafactory_qwen25_1_5b_lora.yaml` 为可执行的训练配置，`llamafactory-cli train train/llamafactory_qwen25_1_5b_lora.yaml` 直接跑。

关键取值与依据：

| 参数 | 取值 | 依据 |
|---|---|---|
| `learning_rate` | 1.0e-4 | LoRA 常用量级；1e-2 会让结构化输出在数十步内崩掉 |
| `cutoff_len` | 1024 | 实测样本最长约 923 token，2048 有一半是 padding |
| 有效批次 | 4 × 2 = 8 | 150 条每轮 19 步，8 轮共 152 步（与 checkpoint-152 一致）|
| `num_train_epochs` | 8 | 3 轮仅约 56 步，收敛不稳；但实测 4.3 轮后 loss 进入平台期，后 4 轮无增益（见下）|
| `lora_rank` / `alpha` | 16 / 32 | 8 字段 + 长数组，容量需求高于单字段分类 |
| `lora_dropout` | 0.05 | 小样本必需的正则 |
| `lora_target` | all | 覆盖全部线性层，收敛快于只挂 q/v |
| `packing` | false | 结构化 JSON 首尾相接会混淆样本边界 |
| `eval_strategy` | no | 留出集仅 20 条，训练期用它选 checkpoint 等于把留出集并入调参 |

训练期观察两点：`plot_loss` 曲线是否在 4~5 轮后走平，以及最终生成的 JSON 解析成功率（解析失败一律按全字段错计分，不修补）。

### 实测（2026-11-17 23:23 起，150 条 / 8 轮）

| 项 | 实测值 |
|---|---|
| 样本 / 轮数 / 有效批次 | 150 / 8 / 4 × 2 = 8 |
| 优化步数 | 152（checkpoint-152） |
| 可训练参数 | 23,404,768（占 1.55B 的 1.51%） |
| 学习率轨迹 | 1e-4 cosine + warmup 0.1，末段约 1.9e-5 |
| train loss | 1.05 轮 0.80 → 2.1 轮 0.20 → 3.2 轮 0.022 → 4.3 轮 0.0016 → 8.0 轮 0.0021 |

平台期判定：step 60（3.2 轮）loss 0.022，step 80（4.3 轮）降到 0.0016，此后 70 步一直在 0.0016~0.0038 之间横盘，末步 0.0021 **高于** step 80。即后 4 轮约 70 步没有带来任何损失下降，属于把 150 条模板样本记住，不是泛化增益。下一轮把 `num_train_epochs` 改成 4.0（约 76 步）即可，训练时间减半、指标预期持平。

若要在训练期就监控，从 `train.json` 里按场景再切 15 条做验证集（**不碰那 5 个留出场景**），配 `eval_strategy: epoch` + `load_best_model_at_end: true` + `early_stopping_patience: 2`；留出集仍然只在训练后评一次。

另：`train loss` 在本任务不能当质量证据。数据是按 20 类故障模式模板合成的，150 条覆盖 15 个场景，8 轮足以逐字复现标注，loss 贴 0 是模板记忆的必然结果。

## 预测产物与独立生成脚本

`do_predict` 会在 `output_dir` 下写两个文件：`predict_results.json`（BLEU/ROUGE）与 `generated_predictions.jsonl`（逐条预测，评分真正要的是这个）。

坑：用 WebUI 的 Predict 页跑时，输出目录被替换成 `saves/<model>/lora/predict_<时间>`，与训练目录 `train_<时间>` 是同级但不同名，容易在训练目录里翻不到。`predict_holdout.yaml` 已把 `output_dir` 写死为 `saves/predict_holdout`。

找不到 jsonl 时不必再猜目录，用独立脚本重生成（不依赖 LLaMA-Factory 的 predict 流程）：

```bash
python train/run_predict.py \
    --model /workspace/Qwen/Qwen2.5-1.5B-Instruct \
    --adapter saves/Qwen2.5-1.5B-Instruct/lora/train_2026-11-17-23-55 \
    --gold train/data/holdout/eval.json \
    --out train/data/holdout/generated_predictions.jsonl
```

脚本与训练侧的对齐点：输入拼法同 alpaca converter（`instruction + "\n" + input`、不加 system）、同一套 chat template、贪心解码、`max_new_tokens=640`。跑完会打印 JSON 可解析条数与耗时，产出 `{"predict": ..., "label": ...}` 逐行格式，可直接喂给 `score_fields.py`。

调试：`--dump-prompt 1` 只打印拼好的 prompt 不加载模型；`--adapter` 不传则评 base 模型，可用于「基线与微调对比」。

## 评测口径

- 字段级 micro-F1：按 14 个叶子字段逐字段比对（含 `symptoms.name`、`runbook.steps` 按值集合展开），归一化后必须完全一致。
- 类别准确率：`root_cause.category`、`severity` 精确匹配，分母为条数。
- 症状名集合 F1、处置步骤召回（相似度 ≥ 0.8，宽松口径，单独标注）。
- 场景级留出指标与同场景变体指标分开报，前者用于判断泛化，后者仅用于回归。
- **严格口径按字段分组报**：结构化字段（无 `change` 时 148 个槽位）与自由文本字段（196 个槽位）分开给命中率。把两者混成一个 micro-F1 会掩盖真实情况——结构化能到 66.9%，自由文本只有 17.9%，合起来 0.39 谁都不知道发生了什么。
- **双口径**：`--fuzzy-threshold 0.8` 只放宽自由文本字段（相似度判定），结构化字段仍严格，两组并排看。
- **可溯源率**：`--grounding` 统计自由文本值是否为原文子串。这是生产口径——字段值必须能从原文摘出来。

### 一个必须知道的数据事实：标注不可溯源

用 gold 当预测跑 `--grounding`，自由文本字段只有 **56/196 = 28.6%** 能在原文找到子串出处，句级相似 ≥0.8 也只到 38.8%。说明**标注是人工改写，不是原文摘录**。

推论：严格逐字口径对自由文本本质不可学（196/344 = 57.0% 的槽位，原文里根本没有那句话）。同场景变体互抄能拿自由文本 100%，只因同场景标注措辞完全一致，不是模型能力。所以看这张表时要始终记住：**自由文本那一列的严格命中率，衡量的是"能否复现标注者的措辞"，不是"抽取是否正确"**。

修法（按优先级）：标注改摘录式 → severity 从生成任务拆出改为规则/指标计算 → 自由文本加后置校验（值必须是原文子串）。

### 为什么不用 BLEU-4 / ROUGE-L 作结论

`do_predict` 只输出 BLEU-4 与 ROUGE-1/2/L。这组指标在本任务是**固定 schema 的结构化输出**，键名与标点约占输出字符的 41%，因此存在三个几乎重合的数值带（本仓库实测，字符级口径）：

| 参照带 | BLEU-4 | ROUGE-L | 含义 |
|---|---|---|---|
| 只输出空壳 schema（键全对、值全空） | 29.2 | 62.0 | 表层指标的下限，此时字段正确率为 0 |
| 不同故障场景之间互比 | 47.6 ~ 51.1 | 59.5 ~ 62.0 | 跨场景模板相似度，即"内容全靠猜"的上限 |
| 同场景不同变体互比 | **96.7** | **98.2** | 指标饱和区，此时症状名 F1 = 1.00、类别准确率 = 1.00，但字段级 micro-F1 仅 **0.73** |

结论：BLEU/ROUGE 只有明显高于"跨场景"且接近"变体"时才携带信息，而落在该区间也无法区分"记住了同场景变体"与"抽对了"。**结构化抽取一律以字段级 micro-F1 与类别准确率为准，BLEU/ROUGE 仅作对照。**

注：LLaMA-Factory 内部若用分词级（jieba）口径，绝对值会与本表略有差异，但三条带的相对关系不变。

## 脚本

| 脚本 | 作用 |
|---|---|
| `audit_leakage.py` | 场景交叠、标签泄漏、数值重合率审计；退出码 1 表示不通过 |
| `split_holdout.py` | 按场景分层留出重新切分，断言校验零交叠 |
| `score_fields.py` | 把 `generated_predictions.jsonl` 换成可报告指标：JSON 有效率、字段级 micro-F1、类别准确率、症状名 F1、步骤召回，并打印上表三条参照带 |

```bash
# 真实预测（LLaMA-Factory 的 do_predict 产物）
python train/score_fields.py --gold train/data/holdout/eval.json \
    --predictions <output_dir>/generated_predictions.jsonl \
    --out train/data/holdout/score_holdout.json

# 指标地板与饱和区，用于解释 BLEU/ROUGE 的可解释区间
python train/score_fields.py --gold train/data/holdout/eval.json  --baseline skeleton
python train/score_fields.py --gold train/data/holdout/train.json --baseline variant
```

评的是哪一套数据必须写清楚：`data/fault_eval.json`（同场景，反映记忆与回归）与 `data/holdout/eval.json`（5 类未见场景，反映泛化）不可混为一谈。
