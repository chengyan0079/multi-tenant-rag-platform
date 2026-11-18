# -*- coding: utf-8 -*-
"""字段级抽取评测：把 LLaMA-Factory 的生成结果换成可报告的指标。

背景：LLaMA-Factory 的 do_predict 只输出 BLEU-4 / ROUGE-1/2/L。这组指标在
「固定 schema 的结构化抽取」任务上不可用作质量结论——
  1) 键名与标点占输出字符约 41%，只输出空壳 schema 就能拿到 ROUGE-L ≈ 64；
  2) 同一故障场景的不同变体之间互比，BLEU-4 均值 96.9，指标本身已饱和；
  3) 它们不检查 JSON 是否合法、字段值是否可被原文支撑。
本脚本给出真正能写的指标：JSON 有效率、字段级 micro-F1、根因类别准确率、
症状名集合 F1、处置步骤召回率，并同时打印 BLEU/ROUGE 的两个参照地板，
用于说明该数值的可解释区间。

用法
----
# 1) 真实预测（需要 LLaMA-Factory 生成的 generated_predictions.jsonl）
python train/score_fields.py --gold train/data/holdout/eval.json \
    --predictions saves/qwen2.5-1.5b-lora-fault-extract/generated_predictions.jsonl \
    --out train/data/holdout/score_holdout.json

# 2) 指标地板：只保留 schema、内容全空（用来说明 BLEU/ROUGE 的下限）
python train/score_fields.py --gold train/data/holdout/eval.json --baseline skeleton

# 3) 指标天花板：同场景变体互比（用来说明 BLEU/ROUGE 的饱和水平）
python train/score_fields.py --gold train/data/holdout/train.json --baseline variant

口径约定
--------
* JSON 解析失败一律按「该条全字段错」计，不修补、不截取部分字段。
* 严格口径 = 归一化后完全一致；长文本字段另报相似度仅作参考，不进主指标。
* 分母一律显式打印，禁止只报百分比。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- 归一化

_WS = re.compile(r"\s+")


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("，", ",").replace("：", ":").replace("；", ";")
    s = _WS.sub("", s)
    return s.lower()


def norm_time(s: str) -> str:
    s = str(s).strip().replace(" ", "T")
    m = re.match(r"(\d{4}-\d{2}-\d{2})T?(\d{2}:\d{2})", s)
    return f"{m.group(1)}T{m.group(2)}" if m else norm_text(s)


def sim(a: str, b: str) -> float:
    """字符级 ROUGE-L F1，仅用于宽松口径的步骤召回。"""
    a, b = norm_text(a), norm_text(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            cur[j] = prev[j - 1] + 1 if a[i - 1] == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    l = prev[len(b)]
    if not l:
        return 0.0
    p, r = l / len(a), l / len(b)
    return 2 * p * r / (p + r)


# ---------------------------------------------------------------- BLEU / ROUGE（与 LLaMA-Factory 对照用）

def _ngrams(s: str, n: int) -> Counter:
    return Counter(s[i : i + n] for i in range(len(s) - n + 1))


def bleu4(pred: str, label: str) -> float:
    scores = []
    for n in range(1, 5):
        gp, gl = _ngrams(pred, n), _ngrams(label, n)
        if not gl:
            scores.append(0.0)
            continue
        ov = sum((gp & gl).values())
        scores.append((ov + 1) / (sum(gp.values()) + 1) if gp else 0.0)
    bp = 1.0 if len(pred) >= len(label) else math.exp(1 - len(label) / max(1, len(pred)))
    if any(s == 0 for s in scores):
        return 0.0
    return bp * math.exp(sum(math.log(s) for s in scores) / 4) * 100


def rouge_l(pred: str, label: str) -> float:
    return sim(pred, label) * 100


# ---------------------------------------------------------------- 扁平化比对

def flatten(obj, prefix: str = "") -> set[tuple[str, str]]:
    """把标注对象摊平成 {(字段路径, 归一化值)}；列表按值展开，天然带集合语义。"""
    out: set[tuple[str, str]] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for v in obj:
            out |= flatten(v, prefix) if isinstance(v, dict) else {(prefix, norm_text(v))}
    else:
        if isinstance(obj, bool):
            val = "true" if obj else "false"
        elif isinstance(obj, (int, float)):
            val = str(obj)
        else:
            val = norm_time(obj) if prefix.endswith("_time") or prefix.endswith(".time") else norm_text(obj)
        out.add((prefix, val))
    return out


def score_pair(gold: dict, pred: dict) -> dict:
    g, p = flatten(gold), flatten(pred)
    tp = len(g & p)
    return {"tp": tp, "fp": len(p - g), "fn": len(g - p)}


# ---------------------------------------------------------------- 双口径
#
# 严格逐字口径对「自由文本」不公平：title / runbook.title / root_cause.description
# / change.content 这类字段，模型换一个说法就整条算错，而这些字段本来就不存在
# 唯一正确写法。所以分两组报：
#   * 结构化字段（单号、枚举、时间戳、类别）→ 只能严格，值域是闭集；
#   * 自由文本字段 → 另给一个「相似度 ≥ 阈值为命中」的口径作参考。
# 主指标仍是严格 micro-F1 与类别准确率，宽松口径只作对照，不能替代。

FREE_TEXT_KEYS = {
    "title",
    "root_cause.description",
    "runbook.title",
    "change.content",
    "symptoms.name",
    "runbook.steps",
}


def _group(items: set[tuple[str, str]]) -> dict[str, list[str]]:
    d: dict[str, list[str]] = {}
    for k, v in items:
        d.setdefault(k, []).append(v)
    return d


def _match_key(gvals: list[str], pvals: list[str], thr: float | None) -> tuple[int, int, int]:
    """单个字段的命中数。thr=None 走严格集合交集；否则贪心按相似度配对。"""
    if thr is None:
        gs, ps = set(gvals), set(pvals)
        m = len(gs & ps)
        return m, len(ps) - m, len(gs) - m
    used = [False] * len(pvals)
    m = 0
    for gv in gvals:
        best, bi = 0.0, -1
        for i, pv in enumerate(pvals):
            if used[i]:
                continue
            s = 1.0 if gv == pv else sim(gv, pv)
            if s > best:
                best, bi = s, i
        if bi >= 0 and best >= thr:
            used[bi] = True
            m += 1
    return m, len(pvals) - m, len(gvals) - m


def score_pair_fuzzy(gold: dict, pred: dict, thr: float) -> dict:
    g, p = _group(flatten(gold)), _group(flatten(pred))
    tp = fp = fn = 0
    for k in set(g) | set(p):
        m, f_p, f_n = _match_key(g.get(k, []), p.get(k, []), thr if k in FREE_TEXT_KEYS else None)
        tp += m
        fp += f_p
        fn += f_n
    return {"tp": tp, "fp": fp, "fn": fn}


SENT = re.compile(r"[。；;\n]+")


def grounding(pred: dict, source: str, thr: float = 0.8) -> tuple[int, int, int]:
    """可溯源率：预测出的自由文本值能否在原文里找到出处。

    返回 (原文子串命中, 句级相似度命中, 自由文本值总数)。生产口径要的是第一个数字——
    字段值必须能从原文摘出来，不是模型自己编。
    """
    src = norm_text(source)
    sents = [norm_text(s) for s in SENT.split(str(source)) if s.strip()]
    ex = lo = tot = 0
    for k, v in flatten(pred):
        if k not in FREE_TEXT_KEYS or not v:
            continue
        tot += 1
        if v in src:
            ex += 1
            lo += 1
        elif sents and any(sim(v, s) >= thr for s in sents):
            lo += 1
    return ex, lo, tot


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


# ---------------------------------------------------------------- 数据读取

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_pred(text: str, allow_fence: bool = True) -> tuple[dict | None, bool]:
    """返回 (对象, 是否用了围栏剥离)。解析失败返回 (None, ...)，不做任何修补。"""
    s = str(text).strip()
    used = False
    if allow_fence:
        t = FENCE.sub("", s)
        if t != s:
            used = True
            s = t.strip()
    m = re.search(r"\{.*\}", s, re.S)
    if m and m.group(0) != s:
        s2 = m.group(0)
        used = True
        s = s2
    try:
        obj = json.loads(s)
        return (obj if isinstance(obj, dict) else None), used
    except Exception:
        return None, used


def load_gold(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        raw = r["output"] if isinstance(r, dict) and "output" in r else r
        obj = raw if isinstance(raw, dict) else json.loads(raw)
        source = ""
        if isinstance(r, dict):
            source = str(r.get("input") or "")
        out.append({"gold": obj,
                    "raw_label": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                    "source": source})
    return out


def load_predictions(path: Path, gold: list[dict], allow_fence: bool) -> list[dict]:
    preds = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            text = r.get("predict") or r.get("prediction") or r.get("output") or ""
            obj, fenced = parse_pred(text, allow_fence)
            preds.append({"gold": None, "raw_label": text, "pred": obj, "fenced": fenced,
                          "label_from_file": r.get("label")})
    if len(preds) != len(gold):
        raise SystemExit(f"预测条数 {len(preds)} 与标注条数 {len(gold)} 不一致，无法逐条对齐")
    for g, p in zip(gold, preds):
        p["gold"] = g["gold"]
        p["source"] = g.get("source", "")
    return preds


# ---------------------------------------------------------------- 基线

def skeleton(obj):
    if isinstance(obj, dict):
        return {k: skeleton(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [skeleton(v) for v in obj]
    if isinstance(obj, bool):
        return False
    if isinstance(obj, (int, float)):
        return 0
    return ""


def baseline_rows(gold: list[dict], kind: str, seed: int = 42) -> list[dict]:
    if kind == "skeleton":
        rows = []
        for g in gold:
            sk = skeleton(g["gold"])
            rows.append({"gold": g["gold"], "pred": sk, "fenced": False,
                         "raw_label": json.dumps(sk, ensure_ascii=False),
                         "source": g.get("source", "")})
        return rows
    # variant：取同一场景的另一条变体当作「预测」，衡量指标饱和水平
    by_title = {}
    for g in gold:
        by_title.setdefault(g["gold"].get("title"), []).append(g["gold"])
    rnd = random.Random(seed)
    rows = []
    for g in gold:
        cands = [c for c in by_title[g["gold"].get("title")] if c is not g["gold"]]
        if cands:
            pick = rnd.choice(cands)
            rows.append({"gold": g["gold"], "pred": pick, "fenced": False,
                         "raw_label": json.dumps(pick, ensure_ascii=False),
                         "source": g.get("source", "")})
    if not rows:
        raise SystemExit("该数据集每个场景只有 1 条，无法做 variant 基线")
    return rows


# ---------------------------------------------------------------- 主评测

def evaluate(rows: list[dict], fuzzy_thr: float = 0.0, do_grounding: bool = False) -> dict:
    n = len(rows)
    ok = [r for r in rows if r["pred"] is not None]
    invalid = n - len(ok)

    tp = fp = fn = 0
    for r in rows:
        if r["pred"] is None:  # 解析失败：该条全字段错
            g = flatten(r["gold"])
            fn += len(set(g))
            continue
        s = score_pair(r["gold"], r["pred"])
        tp += s["tp"]; fp += s["fp"]; fn += s["fn"]

    # 逐字段命中率（严格口径）
    per_field: dict[str, list[int]] = {}
    for r in rows:
        g = flatten(r["gold"])
        p = flatten(r["pred"]) if r["pred"] else set()
        keys = {k for k, _ in g} | {k for k, _ in p}
        for k in keys:
            gk = {x for x in g if x[0] == k}
            pk = {x for x in p if x[0] == k}
            cell = per_field.setdefault(k, [0, 0])
            cell[0] += len(gk & pk)
            cell[1] += len(gk)
    field_rows = []
    for k, (hit, tot) in sorted(per_field.items()):
        field_rows.append({"field": k, "hit": hit, "total": tot,
                           "rate": (hit / tot) if tot else 0.0})

    # 根因类别准确率（分母 = 条数）
    cat_hit = sum(1 for r in rows
                  if r["pred"] and norm_text((r["pred"].get("root_cause") or {}).get("category", ""))
                  == norm_text((r["gold"].get("root_cause") or {}).get("category", "")))
    sev_hit = sum(1 for r in rows
                  if r["pred"] and norm_text(r["pred"].get("severity", ""))
                  == norm_text(r["gold"].get("severity", "")))

    # 症状名集合 P/R/F1
    s_tp = s_fp = s_fn = 0
    for r in rows:
        gn = {norm_text(x.get("name", "")) for x in (r["gold"].get("symptoms") or []) if isinstance(x, dict)}
        pn = {norm_text(x.get("name", "")) for x in ((r["pred"] or {}).get("symptoms") or []) if isinstance(x, dict)}
        s_tp += len(gn & pn); s_fp += len(pn - gn); s_fn += len(gn - pn)

    # 处置步骤召回（宽松：相似度 >= 0.8 视为覆盖）
    step_hit = step_tot = 0
    for r in rows:
        gs = ((r["gold"].get("runbook") or {}).get("steps") or [])
        ps = (((r["pred"] or {}).get("runbook") or {}).get("steps") or [])
        step_tot += len(gs)
        for s in gs:
            if any(sim(s, x) >= 0.8 for x in ps):
                step_hit += 1

    # BLEU / ROUGE 与两个参照地板
    bl, rl = [], []
    for r in rows:
        lab = json.dumps(r["gold"], ensure_ascii=False)
        bl.append(bleu4(r["raw_label"], lab))
        rl.append(rouge_l(r["raw_label"], lab))

    mp, mr, mf = prf(tp, fp, fn)
    sp, sr, sf = prf(s_tp, s_fp, s_fn)

    # 严格口径下的分组统计：结构化字段 vs 自由文本字段
    split = {"structured": [0, 0], "free_text": [0, 0]}
    for r in field_rows:
        b = split["free_text"] if r["field"] in FREE_TEXT_KEYS else split["structured"]
        b[0] += r["hit"]
        b[1] += r["total"]

    # 宽松口径（仅自由文本字段放宽，结构化字段仍严格）
    fuzzy_payload = None
    if fuzzy_thr and fuzzy_thr > 0:
        f_tp = f_fp = f_fn = 0
        f_field = {k: [0, 0] for k in FREE_TEXT_KEYS}
        for r in rows:
            if r["pred"] is None:
                f_fn += len(flatten(r["gold"]))
                continue
            s2 = score_pair_fuzzy(r["gold"], r["pred"], fuzzy_thr)
            f_tp += s2["tp"]; f_fp += s2["fp"]; f_fn += s2["fn"]
            g_g = _group(flatten(r["gold"]))
            p_g = _group(flatten(r["pred"]))
            for k in FREE_TEXT_KEYS:
                if k in g_g:
                    m, _, _ = _match_key(g_g[k], p_g.get(k, []), fuzzy_thr)
                    f_field[k][0] += m
                    f_field[k][1] += len(g_g[k])
        fp2, fr2, ff2 = prf(f_tp, f_fp, f_fn)
        fuzzy_payload = {
            "threshold": fuzzy_thr,
            "field_micro": {"tp": f_tp, "fp": f_fp, "fn": f_fn, "p": fp2, "r": fr2, "f1": ff2},
            "per_field": [{"field": k, "hit": h, "total": t, "rate": (h / t) if t else 0.0}
                          for k, (h, t) in sorted(f_field.items()) if t],
        }

    # 可溯源率
    ground_payload = None
    if do_grounding:
        gx = gl = gt = 0
        for r in rows:
            if r["pred"] is None or not r.get("source"):
                continue
            e, l, t = grounding(r["pred"], r["source"], fuzzy_thr or 0.8)
            gx += e; gl += l; gt += t
        ground_payload = {"exact": gx, "loose": gl, "total": gt,
                          "exact_rate": (gx / gt) if gt else 0.0,
                          "loose_rate": (gl / gt) if gt else 0.0}

    return {
        "n": n,
        "json_valid": len(ok), "json_invalid": invalid,
        "json_valid_rate": len(ok) / n if n else 0.0,
        "fence_stripped": sum(1 for r in rows if r.get("fenced")),
        "field_micro": {"tp": tp, "fp": fp, "fn": fn, "p": mp, "r": mr, "f1": mf},
        "strict_split": {
            "structured": {"hit": split["structured"][0], "total": split["structured"][1],
                           "rate": (split["structured"][0] / split["structured"][1])
                           if split["structured"][1] else 0.0},
            "free_text": {"hit": split["free_text"][0], "total": split["free_text"][1],
                          "rate": (split["free_text"][0] / split["free_text"][1])
                          if split["free_text"][1] else 0.0},
        },
        "field_micro_fuzzy": fuzzy_payload,
        "grounding": ground_payload,
        "root_cause_category": {"hit": cat_hit, "total": n, "acc": cat_hit / n if n else 0.0},
        "severity": {"hit": sev_hit, "total": n, "acc": sev_hit / n if n else 0.0},
        "symptom_name": {"tp": s_tp, "fp": s_fp, "fn": s_fn, "p": sp, "r": sr, "f1": sf},
        "runbook_step_recall": {"hit": step_hit, "total": step_tot,
                                "recall": step_hit / step_tot if step_tot else 0.0},
        "per_field": field_rows,
        "surface": {"bleu4": statistics.mean(bl) if bl else 0.0,
                    "rouge_l": statistics.mean(rl) if rl else 0.0},
    }


def floors(gold: list[dict]) -> dict:
    """两个参照值：schema 地板（只学会结构）与跨场景地板（模板相似度上限）。"""
    sk = [skeleton(g["gold"]) for g in gold]
    lab = [json.dumps(g["gold"], ensure_ascii=False) for g in gold]
    f_schema = statistics.mean(bleu4(json.dumps(a, ensure_ascii=False), b) for a, b in zip(sk, lab))
    f_schema_r = statistics.mean(rouge_l(json.dumps(a, ensure_ascii=False), b) for a, b in zip(sk, lab))
    titles = [g["gold"].get("title") for g in gold]
    pairs = [(i, j) for i, j in itertools.combinations(range(len(gold)), 2) if titles[i] != titles[j]]
    if pairs:
        f_cross = statistics.mean(bleu4(lab[i], lab[j]) for i, j in pairs[:400])
        f_cross_r = statistics.mean(rouge_l(lab[i], lab[j]) for i, j in pairs[:400])
    else:
        f_cross = f_cross_r = 0.0
    return {"schema_floor": {"bleu4": f_schema, "rouge_l": f_schema_r},
            "cross_scenario": {"bleu4": f_cross, "rouge_l": f_cross_r, "pairs": len(pairs[:400])}}


def report(res: dict, fl: dict, tag: str) -> None:
    print(f"\n===== {tag} （n={res['n']}） =====")
    print(f"JSON 有效：{res['json_valid']}/{res['n']}"
          f"（无效 {res['json_invalid']}，解析失败按全字段错计；围栏/多余文本被剥离 {res['fence_stripped']} 条）")
    fm = res["field_micro"]
    print(f"字段级 micro-F1：{fm['f1']:.4f}（P {fm['p']:.4f} / R {fm['r']:.4f}；TP {fm['tp']} FP {fm['fp']} FN {fm['fn']}）")
    rc, se = res["root_cause_category"], res["severity"]
    print(f"根因类别准确率：{rc['hit']}/{rc['total']}（{rc['acc']:.2f}）")
    print(f"严重级别准确率：{se['hit']}/{se['total']}（{se['acc']:.2f}）")
    sn = res["symptom_name"]
    print(f"症状名集合 F1：{sn['f1']:.4f}（P {sn['p']:.4f} / R {sn['r']:.4f}；TP {sn['tp']} FP {sn['fp']} FN {sn['fn']}）")
    st = res["runbook_step_recall"]
    print(f"处置步骤召回（相似度≥0.8 宽松口径）：{st['hit']}/{st['total']}（{st['recall']:.2f}）")
    print("\n-- 逐字段命中（严格口径）--")
    for r in res["per_field"]:
        if r["total"]:
            print(f"  {r['field']:<28} {r['hit']:>4}/{r['total']:<4} {r['rate']*100:>6.1f}%")
    sf = res["surface"]
    print("\n-- 表层指标（与 LLaMA-Factory 输出同类，仅供对照）--")
    print(f"  BLEU-4 {sf['bleu4']:.2f} | ROUGE-L {sf['rouge_l']:.2f}")
    print(f"  参照：只学会 schema、内容全空 → BLEU-4 {fl['schema_floor']['bleu4']:.2f} / ROUGE-L {fl['schema_floor']['rouge_l']:.2f}")
    print(f"  参照：不同场景之间互比（{fl['cross_scenario']['pairs']} 对）→ BLEU-4 {fl['cross_scenario']['bleu4']:.2f} / ROUGE-L {fl['cross_scenario']['rouge_l']:.2f}")
    print("  结论口径：BLEU/ROUGE 只有明显高于这两个参照时才携带信息；"
          "结构化抽取一律以字段级 micro-F1 与类别准确率为准。")

    sp = res.get("strict_split")
    if sp:
        s, t = sp["structured"], sp["free_text"]
        print("\n-- 严格口径拆分 --")
        print(f"  结构化字段（单号/枚举/时间戳/类别）{s['hit']}/{s['total']}（{s['rate'] * 100:.1f}%）")
        print(f"  自由文本字段（标题/描述/症状名/步骤）{t['hit']}/{t['total']}（{t['rate'] * 100:.1f}%）")
        print("  自由文本按逐字比对，换一种说法即判错；该组数值低不代表语义错，见下一段宽松口径。")

    fz = res.get("field_micro_fuzzy")
    if fz:
        fm = fz["field_micro"]
        print(f"\n-- 宽松口径（自由文本相似度 ≥ {fz['threshold']:.2f} 即命中，结构化字段仍严格）--")
        print(f"  字段级 micro-F1：{fm['f1']:.4f}（P {fm['p']:.4f} / R {fm['r']:.4f}；TP {fm['tp']} FP {fm['fp']} FN {fm['fn']}）")
        for r in fz["per_field"]:
            print(f"  {r['field']:<28} {r['hit']:>4}/{r['total']:<4} {r['rate'] * 100:>6.1f}%")

    gd = res.get("grounding")
    if gd and gd.get("total"):
        print("\n-- 可溯源率（自由文本字段值能否在原文找到出处）--")
        print(f"  原文子串命中：{gd['exact']}/{gd['total']}（{gd['exact_rate'] * 100:.1f}%）"
              f" —— 生产口径要的就是这个数")
        print(f"  句级相似 ≥0.8：{gd['loose']}/{gd['total']}（{gd['loose_rate'] * 100:.1f}%）")
        print("  子串命中率高 = 模型在摘录；低 = 模型在改写，需要改成原文片段抽取并加后置校验。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True, help="标注文件（LLaMA-Factory 格式，含 output 字段）")
    ap.add_argument("--predictions", help="generated_predictions.jsonl")
    ap.add_argument("--baseline", choices=["skeleton", "variant"], help="不传预测时用基线代替")
    ap.add_argument("--out", help="结果 json 落盘路径")
    ap.add_argument("--no-allow-fence", action="store_true", help="禁止剥离 ```json 围栏，严格按原文解析")
    ap.add_argument("--fuzzy-threshold", type=float, default=0.0,
                    help="自由文本字段的宽松命中阈值（如 0.8）；0 表示只报严格口径")
    ap.add_argument("--grounding", action="store_true",
                    help="额外统计可溯源率：预测的自由文本值是否为原文子串")
    ap.add_argument("--json", action="store_true", help="只输出 json")
    a = ap.parse_args()

    gold = load_gold(Path(a.gold))
    fl = floors(gold)
    result_sets = []

    if a.predictions:
        rows = load_predictions(Path(a.predictions), gold, not a.no_allow_fence)
        res = evaluate(rows, a.fuzzy_threshold, a.grounding)
        result_sets.append(("真实预测", res))
    for kind in ([a.baseline] if a.baseline else []):
        rows = baseline_rows(gold, kind)
        res = evaluate(rows, a.fuzzy_threshold, a.grounding)
        result_sets.append((f"基线 {kind}", res))

    payload = {"gold": str(a.gold), "predictions": a.predictions,
               "baseline": a.baseline, "floors": fl,
               "results": {tag: r for tag, r in result_sets}}
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        for tag, r in result_sets:
            report(r, fl, tag)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {a.out}")


if __name__ == "__main__":
    main()
