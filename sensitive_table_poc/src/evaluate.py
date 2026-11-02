"""정답셋 대비 평가.

labels.csv 컬럼: table_id, decision(sensitive/non-sensitive), sensitive_columns(| 구분), note
- 테이블 단위: 민감 테이블 탐지 Precision / Recall (보안 관점에서 Recall 우선)
- 컬럼 단위: 민감 컬럼 Precision / Recall (row_condition의 대상 컬럼 포함)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


def _pr(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3)}


def evaluate(results_path: Path, labels_path: Path) -> dict:
    preds = {}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            preds[r["table_id"]] = r
    with open(labels_path, encoding="utf-8-sig", newline="") as f:
        labels = {row["table_id"]: row for row in csv.DictReader(f)}

    t_tp = t_fp = t_fn = t_tn = 0
    c_tp = c_fp = c_fn = 0
    details, missing = [], []
    for tid, lab in labels.items():
        p = preds.get(tid)
        if p is None or p["decision"] == "error":
            missing.append(tid)
            continue
        gold_s = lab["decision"].strip() == "sensitive"
        pred_s = p["decision"] == "sensitive"
        t_tp += gold_s and pred_s
        t_fp += (not gold_s) and pred_s
        t_fn += gold_s and not pred_s
        t_tn += (not gold_s) and not pred_s
        gold_c = {c.strip().upper() for c in (lab.get("sensitive_columns") or "").split("|") if c.strip()}
        pred_c = {s["column_name"].upper() for s in p.get("sensitive_data", [])}
        c_tp += len(gold_c & pred_c)
        c_fp += len(pred_c - gold_c)
        c_fn += len(gold_c - pred_c)
        if gold_s != pred_s or gold_c != pred_c:
            details.append({"table_id": tid, "gold": lab["decision"], "pred": p["decision"],
                            "missed_columns": sorted(gold_c - pred_c),
                            "extra_columns": sorted(pred_c - gold_c),
                            "needs_review": p.get("needs_review")})
    return {
        "table_level": {**_pr(t_tp, t_fp, t_fn), "tn": t_tn},
        "column_level": _pr(c_tp, c_fp, c_fn),
        "evaluated": len(labels) - len(missing),
        "missing_or_error": missing,
        "mismatches": details,
    }


def print_report(rep: dict):
    t, c = rep["table_level"], rep["column_level"]
    print(f"\n[테이블 단위] P={t['precision']} R={t['recall']} F1={t['f1']} "
          f"(TP={t['tp']} FP={t['fp']} FN={t['fn']} TN={t['tn']})")
    print(f"[컬럼 단위]   P={c['precision']} R={c['recall']} F1={c['f1']} "
          f"(TP={c['tp']} FP={c['fp']} FN={c['fn']})")
    print(f"평가 {rep['evaluated']}건, 결과 없음/오류 {len(rep['missing_or_error'])}건")
    for m in rep["mismatches"]:
        print(f"  - {m['table_id']}: gold={m['gold']} pred={m['pred']} "
              f"누락={m['missed_columns']} 과탐={m['extra_columns']}")
