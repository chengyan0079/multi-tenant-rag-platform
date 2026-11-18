# -*- coding: utf-8 -*-
"""独立生成脚本：不依赖 LLaMA-Factory 的 do_predict 产物目录，直接产出
`generated_predictions.jsonl`，交给 score_fields.py 算字段级指标。

什么时候用它
------------
`llamafactory-cli train train/predict_holdout.yaml` 的产物写在 `output_dir`
（`generated_predictions.jsonl` + `predict_results.json`）。用 WebUI 的
Predict 页跑时，输出目录是 WebUI 自己拼的 `saves/<model>/lora/predict_<时间>`，
和训练目录不是同一个，容易找不到。本脚本把「加载 base + 挂 adapter + 逐条生成 +
写 jsonl」四步固定下来，路径自己指定，不再猜。

与 LLaMA-Factory 的对齐点
------------------------
* 输入拼法与 alpaca converter 一致：`instruction + "\\n" + input`，不加 system；
* chat template 用 `tokenizer.apply_chat_template(..., add_generation_prompt=True)`，
  与训练时的 `chat_template.jinja` 同源（Qwen2.5 自带即可）；
* 贪心解码、`max_new_tokens` 默认 640，与 `predict_holdout.yaml` 一致。

用法
----
python train/run_predict.py \
    --model /workspace/Qwen/Qwen2.5-1.5B-Instruct \
    --adapter saves/Qwen2.5-1.5B-Instruct/lora/train_2026-11-17-23-55 \
    --gold train/data/holdout/eval.json \
    --out train/data/holdout/generated_predictions.jsonl

跑完接着：
python train/score_fields.py --gold train/data/holdout/eval.json \
    --predictions train/data/holdout/generated_predictions.jsonl \
    --out train/data/holdout/score_holdout.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def build_prompt(rec: dict) -> str:
    """与 LLaMA-Factory AlpacaDatasetConverter 的拼法一致：prompt + "\\n" + query。"""
    instr = str(rec.get("instruction") or "").strip()
    query = str(rec.get("input") or "").strip()
    return f"{instr}\n{query}" if query else instr


def is_json_obj(text: str) -> bool:
    import re

    s = str(text).strip()
    s = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", s, flags=re.I)
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        s = m.group(0)
    try:
        return isinstance(json.loads(s), dict)
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="base 模型目录，如 /workspace/Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", help="LoRA adapter 目录（含 adapter_config.json）；不传则评 base 模型")
    ap.add_argument("--gold", required=True, help="标注文件（LLaMA-Factory alpaca 格式，含 instruction/input/output）")
    ap.add_argument("--out", default="train/data/holdout/generated_predictions.jsonl")
    ap.add_argument("--max-new-tokens", type=int, default=640)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，调试用")
    ap.add_argument("--dump-prompt", type=int, default=0, help="打印前 N 条完整 prompt 便于核对，不生成")
    ap.add_argument("--cuda", default="auto", help="auto | cuda:0 | cpu")
    a = ap.parse_args()

    rows = json.loads(Path(a.gold).read_text(encoding="utf-8"))
    if a.limit:
        rows = rows[: a.limit]

    if a.dump_prompt:
        for i, r in enumerate(rows[: a.dump_prompt]):
            print(f"\n===== prompt #{i}（{len(build_prompt(r))} 字符）=====\n{build_prompt(r)}")
        return

    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as exc:  # pragma: no cover
        sys.exit(f"缺少依赖：{exc}\n请先 pip install torch transformers peft accelerate")

    use_cuda = a.cuda != "cpu" and torch.cuda.is_available()
    if a.cuda == "cpu" and torch.cuda.is_available():
        print("[warn] 指定了 cpu，20 条 × 640 token 会非常慢，建议用 GPU")
    dtype = torch.bfloat16 if use_cuda else torch.float32

    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        a.model,
        torch_dtype=dtype,
        device_map="auto" if use_cuda else None,
        trust_remote_code=True,
    )
    if a.adapter:
        model = PeftModel.from_pretrained(model, a.adapter)
        print(f"[info] 已挂载 adapter：{a.adapter}")
    else:
        print("[info] 未指定 adapter，评的是 base 模型（可用于基线与微调对比）")
    model.eval()

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    valid_cnt = 0
    with out_path.open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            text = tok.apply_chat_template(
                [{"role": "user", "content": build_prompt(r)}],
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = tok(text, return_tensors="pt").to(model.device)
            with torch.no_grad():
                gen = model.generate(
                    **inputs,
                    max_new_tokens=a.max_new_tokens,
                    do_sample=False,          # 贪心，评测要可复现
                    num_beams=1,
                    pad_token_id=tok.pad_token_id or tok.eos_token_id,
                )
            new_tokens = gen[0][inputs["input_ids"].shape[1]:]
            pred = tok.decode(new_tokens, skip_special_tokens=True).strip()

            ok = is_json_obj(pred)
            valid_cnt += int(ok)
            f.write(json.dumps({"predict": pred, "label": str(r.get("output") or "")},
                               ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{i}/{len(rows)}] {'JSON-OK ' if ok else 'JSON-BAD'} {len(pred):>4} 字符 "
                  f"| {time.time() - t0:6.1f}s")

    dt = time.time() - t0
    print(f"\n已写入 {out_path}")
    print(f"JSON 可解析：{valid_cnt}/{len(rows)}（{valid_cnt / max(1, len(rows)) * 100:.0f}%）；耗时 {dt:.1f}s（{dt / max(1, len(rows)):.1f}s/条）")
    print("\n下一步：")
    print(f"  python train/score_fields.py --gold {a.gold} --predictions {out_path} "
          f"--out train/data/holdout/score_holdout.json")


if __name__ == "__main__":
    main()
