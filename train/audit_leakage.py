#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""训练集 / 留出集泄漏审计。

回答三个问题：
  1. 留出集覆盖多少个独立故障场景，训练集每个场景有多少变体；
  2. 留出集的场景是否在训练集里出现过（标题交集）；
  3. 留出集的标签（症状名）与训练集同场景样本是否一致——这是泄漏的判定依据。

用法：
  python train/audit_leakage.py \
      --train train/data/fault_extract.json \
      --eval  train/data/fault_eval.json

退出码：0 = 通过（无场景交叠）；1 = 存在泄漏。
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        sys.exit("%s 应为 JSON 数组" % path)
    for i, r in enumerate(data):
        if not isinstance(r.get("output"), str):
            sys.exit("%s 第 %d 条缺少 output 字符串" % (path, i))
    return data


def parse_output(rec):
    try:
        return json.loads(rec["output"])
    except ValueError:
        return {}


def symptom_names(obj):
    out = set()
    for s in obj.get("symptoms") or []:
        if isinstance(s, dict) and s.get("name"):
            out.add(str(s["name"]).strip())
    return out


def strip_ids(text):
    text = re.sub(r"INC-\d{4}-\d{3,}", "", text)
    return re.sub(r"\d+", "<N>", text)


def digits(text):
    return set(re.findall(r"\d+(?:\.\d+)?", strip_ids(text)))


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a | b) else 1.0


def main():
    ap = argparse.ArgumentParser(description="训练集/留出集泄漏审计")
    ap.add_argument("--train", default="train/data/fault_extract.json")
    ap.add_argument("--eval", default="train/data/fault_eval.json")
    args = ap.parse_args()

    train = load(args.train)
    evals = load(args.eval)

    tr_by_title = defaultdict(list)
    for r in train:
        tr_by_title[parse_output(r).get("title") or "<无标题>"].append(r)

    tr_titles = set(tr_by_title)
    ev_objs = [parse_output(r) for r in evals]
    ev_titles = set(o.get("title") or "<无标题>" for o in ev_objs)

    print("=" * 66)
    print("训练集 %d 条 / 留出集 %d 条" % (len(train), len(evals)))
    print("=" * 66)

    per_scene = Counter(len(v) for v in tr_by_title.values())
    print("训练集场景数 %d，每场景变体数分布：%s" % (
        len(tr_by_title), dict(sorted(per_scene.items()))))

    sev_tr = Counter(parse_output(r).get("severity") for r in train)
    sev_ev = Counter(o.get("severity") for o in ev_objs)
    print("严重度分布  训练 %s | 留出 %s" % (dict(sev_tr), dict(sev_ev)))

    overlap = ev_titles & tr_titles
    print("")
    print("场景交叠：留出集 %d 个场景，其中 %d 个在训练集出现过" % (
        len(ev_titles), len(overlap)))
    if overlap:
        for t in sorted(overlap):
            print("  - %s（训练集 %d 条变体）" % (t, len(tr_by_title[t])))

    # 标签级泄漏：同场景症状名是否一致
    same_label = 0
    jac = []
    for o in ev_objs:
        t = o.get("title") or "<无标题>"
        if t not in tr_by_title:
            continue
        a = symptom_names(o)
        best = max((jaccard(a, symptom_names(parse_output(r))) for r in tr_by_title[t]), default=0.0)
        jac.append(best)
        if abs(best - 1.0) < 1e-9:
            same_label += 1

    if jac:
        print("")
        print("标签泄漏：留出集 %d 条中，%d 条的症状名集合与其同场景训练样本完全一致" % (
            len(jac), same_label))
        print("  症状名 Jaccard 均值 %.2f，最低 %.2f" % (sum(jac) / len(jac), min(jac)))
        num = []
        for r, o in zip(evals, ev_objs):
            t = o.get("title") or "<无标题>"
            for tr in tr_by_title.get(t, []):
                if tr is r:
                    continue
                q = digits(r.get("input", ""))
                if q:
                    num.append(len(q & digits(tr.get("input", ""))) / len(q))
        if num:
            print("  输入数值重合率均值 %.1f%%，最低 %.1f%%（变体只替换了时间与单号）" % (
                sum(num) / len(num) * 100, min(num) * 100))

    print("")
    verdict = "通过：留出集与训练集无共享场景" if not overlap else "不通过：留出集场景在训练集中出现过，指标反映记忆而非泛化"
    print("结论：%s" % verdict)
    return 0 if not overlap else 1


if __name__ == "__main__":
    sys.exit(main())
