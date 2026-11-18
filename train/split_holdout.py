#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按“故障场景”重新切分训练集与留出集（场景级留出，避免同场景变体泄漏）。

原数据的切分方式是按 fault_id，但同一场景有 10 条变体，只改单号与时间，
因此留出集与训练集共享全部 20 个场景，指标反映记忆而非泛化。
本脚本改为：整组场景留出，留出场景在训练集中一条都不出现。

用法：
  python train/split_holdout.py --holdout 5 --per-scenario 4 --seed 42
  python train/split_holdout.py --holdout 5 --per-scenario 0      # 留出场景的全部变体

产出（默认 train/data/holdout/）：
  train.json / eval.json / dataset_info.json / split_report.json
"""

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def title_of(rec):
    try:
        return json.loads(rec["output"]).get("title") or "<无标题>"
    except ValueError:
        return "<无标题>"


def severity_of(rec):
    try:
        return json.loads(rec["output"]).get("severity") or "P?"
    except ValueError:
        return "P?"


def main():
    ap = argparse.ArgumentParser(description="场景级留出切分")
    ap.add_argument("--src", default="train/data/fault_extract.json")
    ap.add_argument("--holdout", type=int, default=5, help="留出的场景个数")
    ap.add_argument("--per-scenario", type=int, default=4,
                    help="每个留出场景取几条做留出集，0 表示全部")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", default="train/data/holdout")
    args = ap.parse_args()

    src = load(args.src)
    groups = defaultdict(list)
    for r in src:
        groups[title_of(r)].append(r)

    scenes = sorted(groups)
    if args.holdout >= len(scenes):
        sys.exit("留出场景数必须小于场景总数 %d" % len(scenes))

    # 按严重度分层抽取，保证留出集仍覆盖 P1/P2/P3
    by_sev = defaultdict(list)
    for t in scenes:
        by_sev[Counter(severity_of(r) for r in groups[t]).most_common(1)[0][0]].append(t)

    rnd = random.Random(args.seed)
    for v in by_sev.values():
        rnd.shuffle(v)

    # 按场景数比例分配留出配额，并保证每个严重度至少 1 个场景
    total = sum(len(v) for v in by_sev.values())
    quota = {sev: int(round(args.holdout * len(v) / total)) for sev, v in by_sev.items()}
    if args.holdout >= len(by_sev):
        for sev in by_sev:
            quota[sev] = max(quota[sev], 1)
    for sev in quota:
        quota[sev] = min(quota[sev], len(by_sev[sev]))

    while sum(quota.values()) > args.holdout:
        sev = max((s for s in quota if quota[s] > 1), key=lambda s: quota[s])
        quota[sev] -= 1
    while sum(quota.values()) < args.holdout:
        sev = max((s for s in quota if quota[s] < len(by_sev[s])), key=lambda s: len(by_sev[s]) - quota[s])
        quota[sev] += 1

    hold = []
    for sev, n in quota.items():
        hold.extend(by_sev[sev][:n])

    hold_set = set(hold)
    eval_recs = []
    for t in sorted(hold_set):
        pool = groups[t]
        rnd.shuffle(pool)
        take = pool if args.per_scenario == 0 else pool[:args.per_scenario]
        eval_recs.extend(take)

    train_recs = [r for t in scenes if t not in hold_set for r in groups[t]]

    # 校验：场景零交叠
    tr_titles = set(title_of(r) for r in train_recs)
    ev_titles = set(title_of(r) for r in eval_recs)
    assert not (tr_titles & ev_titles), "切分仍存在场景交叠，请检查逻辑"

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "train.json"), "w", encoding="utf-8") as f:
        json.dump(train_recs, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, "eval.json"), "w", encoding="utf-8") as f:
        json.dump(eval_recs, f, ensure_ascii=False, indent=1)

    base = os.path.basename(args.out_dir)
    info = {
        "fault_extract_holdout": {
            "file_name": "%s/train.json" % base,
            "columns": {"prompt": "instruction", "query": "input", "response": "output"},
        },
        "fault_extract_holdout_eval": {
            "file_name": "%s/eval.json" % base,
            "columns": {"prompt": "instruction", "query": "input", "response": "output"},
        },
    }
    with open(os.path.join(args.out_dir, "dataset_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    report = {
        "seed": args.seed,
        "scene_total": len(scenes),
        "holdout_scenes": sorted(hold_set),
        "train_records": len(train_recs),
        "eval_records": len(eval_recs),
        "train_scenes": len(tr_titles),
        "eval_scenes": len(ev_titles),
        "train_severity": dict(Counter(severity_of(r) for r in train_recs)),
        "eval_severity": dict(Counter(severity_of(r) for r in eval_recs)),
        "scene_overlap": 0,
    }
    with open(os.path.join(args.out_dir, "split_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 66)
    print("场景级切分完成")
    print("=" * 66)
    print("留出场景 %d 个（%s）" % (len(hold_set), ",".join(report["train_severity"]) + " 分布见下"))
    for t in sorted(hold_set):
        print("  - %s" % t)
    print("")
    print("训练集 %d 条（%d 个场景）  %s" % (
        len(train_recs), len(tr_titles), report["train_severity"]))
    print("留出集 %d 条（%d 个场景）  %s" % (
        len(eval_recs), len(ev_titles), report["eval_severity"]))
    print("场景交叠 0（已断言校验）")
    print("")
    print("产出目录：%s" % args.out_dir)
    print("LLaMA-Factory 数据集名：fault_extract_holdout / fault_extract_holdout_eval")
    print("下一步：重训 LoRA → 用留出集重评 → 用新指标替换简历中的 12/20、0.71→0.84")


if __name__ == "__main__":
    main()
