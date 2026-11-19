#!/usr/bin/env python3
"""Data Lake 민감 테이블 식별 PoC — 실행 진입점.

실행: python app.py
- 입력: data/datalake_crawling_result_{스키마}.txt, data/keywords.txt
- 출력: output/results.jsonl, output/result_MMDDHHMM.xlsx
- 실행하면 브라우저에 로컬 UI(ui.py)가 열린다
- LLM 연결 정보는 환경변수(LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)로 지정
"""
from __future__ import annotations

import logging
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
SCHEMAS = ["cim", "erp", "mmd", "pis", "scm", "hq1"]   # 선택 가능한 스키마 (첫 번째가 기본값)
KEYWORDS_FILE = ROOT / "data" / "keywords.txt"
OUT_DIR = ROOT / "output"
WORKERS = 4                     # 동시에 처리할 테이블 수


def tables_file(schema: str) -> Path:
    """스키마별 크롤링 결과 파일 경로: data/datalake_crawling_result_{schema}.txt"""
    return ROOT / "data" / f"datalake_crawling_result_{schema}.txt"


def run(schema: str = SCHEMAS[0], model: str | None = None, reasoning_effort: str | None = None,
        on_result=None, stop_event=None) -> Path | None:
    """파이프라인 전체 실행. 결과 엑셀 경로를 돌려준다.

    schema: 읽어 올 크롤링 결과 파일의 스키마명 (예: scm → datalake_crawling_result_scm.txt)
    model, reasoning_effort: UI에서 고른 값. None이면 환경변수(LLM_MODEL 등) 값을 쓴다

    on_result : 테이블 1건이 끝날 때마다 호출할 함수 (UI 진행 상황 표시용)
    stop_event: threading.Event. 설정되면 아직 시작하지 않은 테이블은 취소하고 멈춘다
    """
    table_path = tables_file(schema)
    missing = [str(p) for p in (table_path, KEYWORDS_FILE) if not p.exists()]
    if missing:
        logging.error("입력 파일이 없습니다: %s", ", ".join(missing))
        return None
    s = load_settings(model=model, reasoning_effort=reasoning_effort)
    keywords = load_keywords(KEYWORDS_FILE)
    tables = load_tables(table_path)
    logging.info("스키마 %s · 키워드 %d개, 대상 테이블 %d개 · 모델 %s · reasoning_effort %s",
                 schema, len(keywords), len(tables), s.model, getattr(s, "reasoning_effort", "-"))

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
            if on_result:
                on_result(r)
            if stop_event is not None and stop_event.is_set():
                for pending in futs:
                    pending.cancel()        # 아직 시작하지 않은 테이블은 취소 (처리 중인 건은 끝까지 기다림)
                logging.warning("사용자가 실행을 중지함: %d/%d 처리 후 중단", i, len(tables))
                break
    summary = write_summary(OUT_DIR)
    logging.info("완료: %s", dict(counts))
    logging.info("결과 파일: %s", summary)
    return summary


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
    """UI 없이 터미널에서만 실행할 때 사용 (python -c "import app; app.main()")."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    start = time.perf_counter()
    try:
        return 0 if run() else 1
    finally:
        print(f"\n총 소요 시간: {format_elapsed(time.perf_counter() - start)}")


if __name__ == "__main__":
    from ui import launch      # python app.py → 로컬 UI 실행
    launch()
