"""결과 저장: results.jsonl(상세, 이어쓰기) + result_MMDDHHMM.csv(검토용, Excel 호환)."""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path

from .models import TableResult


def load_done_ids(out_dir: Path) -> set[str]:
    p = out_dir / "results.jsonl"
    if not p.exists():
        return set()
    done = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("decision") != "error":   # 오류 건은 재처리
                done.add(r["table_id"])
    return done


class ResultWriter:
    def __init__(self, out_dir: Path, append: bool):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / "results.jsonl"
        self._f = open(self.path, "a" if append else "w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, r: TableResult):
        with self._lock:
            self._f.write(r.model_dump_json() + "\n")
            self._f.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


def format_tags(result: dict) -> tuple[str, str]:
    """high_risk_tags를 CSV용 (유형명 "|" 결합, "유형: 근거" 결합) 두 문자열로 바꾼다."""
    tags = result.get("high_risk_tags", [])
    names = "|".join(t["tag"] for t in tags)
    reasons = " | ".join(f'{t["tag"]}: {t["reason"]}' for t in tags)
    return names, reasons


def write_summary(out_dir: Path) -> Path:
    latest: dict[str, dict] = {}
    for line in (out_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            latest[r["table_id"]] = r          # 재처리 시 마지막 결과 우선
    timestamp = datetime.now().strftime("%m%d%H%M")   # 월·일·시·분, 예: 11131401
    path = out_dir / f"result_{timestamp}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table_id", "sys_cd", "decision", "confidence",
                    "sensitive_columns", "row_conditions", "matched_keywords",
                    "high_risk_tag", "high_risk_tag_reason",                    # tagger 결과
                    "needs_review", "review_reasons", "reason", "error"])
        for r in latest.values():
            sd = r.get("sensitive_data", [])
            tag_names, tag_reasons = format_tags(r)
            w.writerow([
                r["table_id"], r["sys_cd"], r["decision"], r.get("confidence"),
                " | ".join(s["column_name"] for s in sd if s["type"] == "column"),
                " | ".join(s["row_condition"] or "" for s in sd if s["type"] == "row_condition"),
                " | ".join(sorted({f'{m["keyword_id"]}:{m["keyword"]}' for s in sd for m in s["matched_keywords"]})),
                tag_names, tag_reasons,                                         # tagger 결과
                r.get("needs_review"), " | ".join(r.get("review_reasons", [])),
                r.get("reason", ""), r.get("error") or "",
            ])
    return path


def write_prompts(out_dir: Path, system_prompt: str, rendered: dict[str, list[str]]) -> Path:
    d = out_dir / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "_system_prompt.md").write_text(system_prompt, encoding="utf-8")
    for table_id, users in rendered.items():
        for i, u in enumerate(users, 1):
            suffix = f"_part{i}" if len(users) > 1 else ""
            (d / f"{table_id}{suffix}.user.md").write_text(u, encoding="utf-8")
    return d
