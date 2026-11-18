#!/usr/bin/env python3
"""Data Lake 민감 테이블 식별 PoC — 실행 진입점.

실행: python app.py
- 입력: data/datalake_crawling_result.txt, data/keywords.txt
- 출력: output/results.jsonl, output/result_MMDDHHMM.csv
- LLM 연결 정보는 환경변수(LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)로 지정
"""
from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.classifier import SensitiveTableClassifier
from src.config import load_settings
from src.llm_client import create_client
from src.loader import load_keywords, load_tables
from src.writer import ResultWriter, write_summary

# ── 실행 설정 (필요하면 여기 값만 바꾼다) ──────────────────
ROOT = Path(__file__).parent
TABLES_FILE = ROOT / "data" / "datalake_crawling_result.txt"
KEYWORDS_FILE = ROOT / "data" / "keywords.txt"
OUT_DIR = ROOT / "output"
WORKERS = 4                     # 동시에 처리할 테이블 수


def run() -> int:
    missing = [str(p) for p in (TABLES_FILE, KEYWORDS_FILE) if not p.exists()]
    if missing:
        print("입력 파일이 없습니다:\n  " + "\n  ".join(missing))
        print("data/ 폴더에 datalake_crawling_result.txt, keywords.txt를 넣어주세요.")
        return 1
    s = load_settings()
    keywords = load_keywords(KEYWORDS_FILE)
    tables = load_tables(TABLES_FILE)
    logging.info("키워드 %d개, 대상 테이블 %d개", len(keywords), len(tables))

    clf = SensitiveTableClassifier(keywords, s, create_client(s))
    counts: Counter = Counter()
    with ResultWriter(OUT_DIR, append=False) as w, ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(clf.classify, t): t for t in tables}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            w.write(r)
            counts[r.decision] += 1
            counts["needs_review"] += r.needs_review
            logging.info("[%d/%d] %-32s %-13s conf=%.2f review=%s", i, len(tables),
                         r.table_id, r.decision, r.confidence, r.needs_review)
    summary = write_summary(OUT_DIR)
    print(f"\n완료: {dict(counts)}")
    print(f"결과: {OUT_DIR / 'results.jsonl'}\n요약: {summary}")
    return 0


def format_elapsed(seconds: float) -> str:
    """초를 '1시간 2분 3.4초' 형태로 바꾼다."""
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}시간 {minutes}분 {sec:.1f}초"
    if minutes:
        return f"{minutes}분 {sec:.1f}초"
    return f"{sec:.1f}초"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    start = time.perf_counter()
    try:
        return run()
    finally:
        print(f"\n총 소요 시간: {format_elapsed(time.perf_counter() - start)}")


if __name__ == "__main__":
    sys.exit(main())
