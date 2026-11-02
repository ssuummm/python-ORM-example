"""프롬프트 조립.

- 시스템 프롬프트: 역할·판단 기준·출력 스키마 + 전사 키워드(전 테이블 공통) → 프롬프트 캐싱 대상
- 사용자 프롬프트: 시스템별 키워드 + 사전 매칭 힌트 + 테이블 메타데이터 (테이블마다 변경)
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import Keyword

PROMPT_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def format_keywords(kws: list[Keyword]) -> str:
    if not kws:
        return "(없음)"
    lines = []
    for k in kws:
        parts = [k.keyword_id, k.keyword]
        if k.aliases:
            parts.append("별칭: " + ", ".join(k.aliases))
        if k.category:
            parts.append("분류: " + k.category)
        parts.append("등급: " + k.level)
        if k.description:
            parts.append("설명: " + k.description)
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def build_system_prompt(global_kws: list[Keyword]) -> str:
    return _load("system_prompt.md").replace("{{GLOBAL_KEYWORDS}}", format_keywords(global_kws))


def build_user_prompt(system_code: str, system_kws: list[Keyword], payload: dict,
                      hints_text: str, chunk_no: int, chunk_total: int) -> str:
    if chunk_total > 1:
        chunk_info = (f"※ 이 테이블은 컬럼 수가 많아 {chunk_total}개 묶음으로 나누어 판단한다. "
                      f"지금은 {chunk_no}/{chunk_total}번째 묶음이며, 제공된 컬럼만 판단하라.")
    else:
        chunk_info = "※ 테이블 전체 컬럼이 제공되었다."
    return (_load("user_prompt.md")
            .replace("{{SYSTEM_CODE}}", system_code)
            .replace("{{SYSTEM_KEYWORDS}}", format_keywords(system_kws))
            .replace("{{PREFILTER_HINTS}}", hints_text)
            .replace("{{CHUNK_INFO}}", chunk_info)
            .replace("{{TABLE_JSON}}", compact_payload(payload)))


def compact_payload(payload: dict) -> str:
    """컬럼 1개 = 1줄. 들여쓰기 JSON 대비 토큰을 크게 줄이면서 가독성 유지."""
    head = {k: v for k, v in payload.items() if k != "columns"}
    cols = ",\n  ".join(json.dumps(c, ensure_ascii=False) for c in payload["columns"])
    body = json.dumps(head, ensure_ascii=False)[:-1]
    return f'{body}, "columns": [\n  {cols}\n]}}'


RETRY_SUFFIX = (
    "\n\n[재요청] 직전 응답을 처리하지 못했다. 오류: {error}\n"
    "설명 없이, 시스템 프롬프트의 출력 스키마를 정확히 따르는 JSON 객체 하나만 다시 출력하라."
)
