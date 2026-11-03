#!/usr/bin/env python3
"""Data Lake 민감 테이블 식별 PoC — CLI 진입점.

예)
  # 1) 프롬프트만 생성해서 확인 (LLM 호출 없음)
  python app.py run --dry-run

  # 2) 오프라인 파이프라인 점검 (규칙 기반 mock)
  python app.py run --provider mock

  # 3) 사내 LLM Gateway(OpenAI 호환)로 실행
  export LLM_BASE_URL=https://llm-gw.example.internal/v1 LLM_MODEL=<model> LLM_API_KEY=<key>
  python app.py run --provider openai --workers 4
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.classifier import SensitiveTableClassifier
from src.config import load_settings
from src.llm_client import create_client
from src.loader import load_keywords, load_tables
from src.writer import ResultWriter, load_done_ids, write_prompts, write_summary

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DEFAULT_TABLES = DATA / "datalake_crawling_result.txt"
DEFAULT_KEYWORDS = DATA / "keywords.txt"


def cmd_run(a: argparse.Namespace) -> int:
    missing = [p for p in (a.tables, a.keywords) if not Path(p).exists()]
    if missing:
        print("입력 파일이 없습니다:\n  " + "\n  ".join(missing))
        print("data/ 폴더에 datalake_crawling_result.txt, keywords.txt를 넣어주세요.")
        return 1
    s = load_settings(provider=a.provider, max_columns_per_call=a.max_columns,
                      send_raw_values=True if a.send_raw_values else None)
    keywords = load_keywords(a.keywords)
    tables = load_tables(a.tables)
    if a.system:
        tables = [t for t in tables if t.sys_cd == a.system]
    if a.table:
        tables = [t for t in tables if t.table_id == a.table or t.table_name == a.table]
    if a.limit:
        tables = tables[: a.limit]
    out = Path(a.out_dir)
    logging.info("키워드 %d개, 대상 테이블 %d개", len(keywords), len(tables))

    if a.dry_run:
        clf = SensitiveTableClassifier(keywords, s, client=None)
        d = write_prompts(out, clf.system_prompt, {t.table_id: clf.render_prompts(t) for t in tables})
        print(f"프롬프트 생성 완료: {d}")
        return 0

    clf = SensitiveTableClassifier(keywords, s, create_client(s))
    if a.resume:
        done = load_done_ids(out)
        tables = [t for t in tables if t.table_id not in done]
        logging.info("이어하기: 완료 %d건 제외, 남은 %d건", len(done), len(tables))

    counts: Counter = Counter()
    with ResultWriter(out, append=a.resume) as w, ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(clf.classify, t): t for t in tables}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            w.write(r)
            counts[r.decision] += 1
            counts["needs_review"] += r.needs_review
            logging.info("[%d/%d] %-32s %-13s conf=%.2f review=%s", i, len(tables),
                         r.table_id, r.decision, r.confidence, r.needs_review)
    summary = write_summary(out)
    print(f"\n완료: {dict(counts)}")
    print(f"결과: {out / 'results.jsonl'}\n요약: {summary}")
    return 0


def main(argv=None) -> int:
    # 하위 명령 없이 `python app.py [옵션]`으로 실행하면 run으로 처리
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in ("run", "-h", "--help"):
        argv = ["run", *argv]

    p = argparse.ArgumentParser(description="Data Lake 민감 테이블 식별 PoC")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="민감 테이블 판단 실행 (기본 명령)")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--tables", default=str(DEFAULT_TABLES), help="테이블 메타데이터 (기본: data/datalake_crawling_result.txt)")
    r.add_argument("--keywords", default=str(DEFAULT_KEYWORDS), help="기본: data/keywords.txt")
    r.add_argument("--out-dir", default=str(ROOT / "output"))
    r.add_argument("--provider", choices=["mock", "openai", "anthropic"], default=None,
                   help="미지정 시 LLM_PROVIDER 환경변수(기본 mock)")
    r.add_argument("--workers", type=int, default=4, help="동시 처리 테이블 수 (기본 4)")
    r.add_argument("--max-columns", type=int, default=None, help="호출당 최대 컬럼 수")
    r.add_argument("--system", help="특정 system_code만")
    r.add_argument("--table", help="특정 테이블만 (table_id 또는 table_name)")
    r.add_argument("--limit", type=int)
    r.add_argument("--resume", action="store_true", help="results.jsonl에 있는 테이블은 건너뜀")
    r.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 프롬프트만 생성")
    r.add_argument("--send-raw-values", action="store_true", help="[확인 필요] 샘플 값 마스킹 해제")
    r.set_defaults(func=cmd_run)


    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
