"""LLM 호출 전 전처리.

1) 규칙 기반 키워드 사전 매칭 → LLM 힌트 + 후처리 교차검증에 사용
2) 샘플 값 프로파일링/마스킹 → 원본 민감값이 LLM으로 나가지 않도록
3) 컬럼이 많은 테이블 분할
"""
from __future__ import annotations

import re
from collections import defaultdict

from .config import Settings
from .models import TABLE_TARGET, ColumnMeta, Keyword, KeywordHit, TableMeta

# ---------------------------------------------------------------------------
# 1. 키워드 사전 매칭
# ---------------------------------------------------------------------------
_SEP = re.compile(r"[\s_\-\.]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")


def normalize(text: str) -> str:
    return _SEP.sub("", text or "").upper()


def identifier_tokens(name: str) -> set[str]:
    """RRN_ENC → {RRN, ENC}, salaryAmt → {SALARY, AMT}"""
    name = _CAMEL.sub(r"\1_\2", name or "")
    return {t.upper() for t in _SEP.split(name) if t}


def _is_short_ascii(term_norm: str) -> bool:
    # SAL, YLD, PWD 같은 3자 이하 영문 약어는 부분문자열 매칭 시 오탐이 많으므로 토큰 단위로만 매칭
    return term_norm.isascii() and len(term_norm) <= 3


class KeywordMatcher:
    def __init__(self, keywords: list[Keyword]):
        self._terms: list[tuple[Keyword, str, str, bool]] = []
        for kw in keywords:
            for term in kw.terms:
                n = normalize(term)
                if n:
                    self._terms.append((kw, term, n, _is_short_ascii(n)))

    def _match_text(self, text: str, is_identifier: bool) -> list[tuple[Keyword, str]]:
        if not text:
            return []
        norm = normalize(text)
        toks = identifier_tokens(text) if is_identifier else set()
        found: dict[str, tuple[Keyword, str]] = {}
        for kw, term, n, short in self._terms:
            if kw.keyword_id in found:
                continue
            if short:
                if is_identifier and n in toks:
                    found[kw.keyword_id] = (kw, term)
            elif n in norm:
                found[kw.keyword_id] = (kw, term)
        return list(found.values())

    def find_hits(self, table: TableMeta, max_values: int = 20) -> list[KeywordHit]:
        hits: list[KeywordHit] = []
        seen: set[tuple[str, str, str]] = set()

        def add(target: str, field: str, text: str, is_identifier: bool):
            for kw, term in self._match_text(text, is_identifier):
                key = (kw.keyword_id, target, field)
                if key not in seen:
                    seen.add(key)
                    hits.append(KeywordHit(keyword_id=kw.keyword_id, term=term, target=target, field=field))

        add(TABLE_TARGET, "table_name", table.table_name, True)
        add(TABLE_TARGET, "table_desc", table.table_desc, False)
        for c in table.columns:
            add(c.column_name, "column_name", c.column_name, True)
            add(c.column_name, "column_desc", c.column_desc, False)
            add(c.column_name, "std_term", c.std_term, False)
            add(c.column_name, "std_term_desc", c.std_term_desc, False)
            for v in c.sample_values[:max_values]:      # 원본 값 기준 매칭 (로컬에서만 수행)
                add(c.column_name, "value", v, True)
        return hits


def hits_by_target(hits: list[KeywordHit]) -> dict[str, list[KeywordHit]]:
    g: dict[str, list[KeywordHit]] = defaultdict(list)
    for h in hits:
        g[h.target].append(h)
    return g


def format_hints(hits: list[KeywordHit], kw_index: dict[str, Keyword]) -> str:
    if not hits:
        return "(사전 매칭된 키워드 없음 — 키워드의 의미·동의어·약어 관점에서 직접 판단할 것)"
    lines = []
    for target, hs in hits_by_target(hits).items():
        label = "[테이블]" if target == TABLE_TARGET else target
        per_kw: dict[str, list[str]] = defaultdict(list)
        for h in hs:
            per_kw[h.keyword_id].append(f"{h.field}~'{h.term}'")
        parts = [f"{kid}({kw_index[kid].keyword}) ← {', '.join(v)}" for kid, v in per_kw.items() if kid in kw_index]
        lines.append(f"- {label}: " + "; ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. 샘플 값 프로파일링 / 마스킹
# ---------------------------------------------------------------------------
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("KR_RRN", re.compile(r"^\d{6}-?[1-8]\d{6}$")),
    ("EMAIL", re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")),
    ("KR_MOBILE", re.compile(r"^01[016789]-?\d{3,4}-?\d{4}$")),
    ("CARD_NO", re.compile(r"^\d{4}-?\d{4}-?\d{4}-?\d{4}$")),
    ("IPV4", re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")),
    ("HASH_OR_ENCRYPTED", re.compile(r"^(?:[A-Fa-f0-9]{32,}|\$2[aby]\$.{50,}|[A-Za-z0-9+/]{24,}={0,2})$")),
]
_CODE_COL_SUFFIX = re.compile(r"_(CD|CODE|TYPE|TP|GB|GBN|GRADE|GRD|LVL|LEVEL|YN|FLAG|FLG|STAT|STATUS|CLS)$", re.I)
_CODE_STD_SUFFIX = ("코드", "구분", "등급", "여부", "유형", "상태")


def detect_pattern(v: str) -> str | None:
    for tag, p in PII_PATTERNS:
        if p.match(v.strip()):
            return tag
    return None


def shape(v: str, limit: int = 40) -> str:
    """값의 형태만 남긴다: 숫자→9, 영문→X, 한글→가 (구분자·기호는 유지)."""
    out = []
    for ch in v[:limit]:
        if ch.isdigit():
            out.append("9")
        elif ch.isascii() and ch.isalpha():
            out.append("X")
        elif "가" <= ch <= "힣":
            out.append("가")
        else:
            out.append(ch)
    return "".join(out) + ("…" if len(v) > limit else "")


def is_code_column(c: ColumnMeta) -> bool:
    """코드/구분/등급 컬럼은 값 자체가 row 조건의 근거가 되므로 원문 유지."""
    return bool(_CODE_COL_SUFFIX.search(c.column_name)) or c.std_term.endswith(_CODE_STD_SUFFIX)


def profile_values(c: ColumnMeta, has_hit: bool, settings: Settings) -> tuple[list[str], list[str]]:
    vals = c.sample_values[: settings.max_sample_values]
    patterns: set[str] = set()
    all_distinct = len(vals) > 1 and len(set(vals)) == len(vals)
    code_col = is_code_column(c)
    out: list[str] = []
    for v in vals:
        tag = detect_pattern(v)
        if tag:
            patterns.add(tag)
        if settings.send_raw_values:
            out.append(v[:60])
        elif tag == "HASH_OR_ENCRYPTED":
            out.append(f"[{tag}]")
        elif tag:
            out.append(f"[{tag}] {shape(v)}")
        elif has_hit and all_distinct and not code_col:
            # 키워드가 걸린 컬럼의 고유값(금액, 이름, 사번 등)은 형태만 전달
            out.append(shape(v))
        else:
            out.append(v[:60])
    return list(dict.fromkeys(out)), sorted(patterns)


def build_table_payload(table: TableMeta, hits: list[KeywordHit], settings: Settings) -> dict:
    hit_cols = {h.target for h in hits if h.target != TABLE_TARGET}
    cols = []
    for c in table.columns:
        values, patterns = profile_values(c, c.column_name in hit_cols, settings)
        d: dict = {"name": c.column_name}
        for key, val in (("desc", c.column_desc), ("std_term", c.std_term),
                         ("std_term_desc", c.std_term_desc), ("type", c.data_type)):
            if val:
                d[key] = val
        if values:
            d["values"] = values
        if patterns:
            d["value_patterns"] = patterns
        cols.append(d)
    return {
        "table_id": table.table_id,
        "system_code": table.system_code,
        "table_name": table.table_name,
        "table_desc": table.table_desc,
        "columns": cols,
    }


# ---------------------------------------------------------------------------
# 3. 분할
# ---------------------------------------------------------------------------
def chunk_table(table: TableMeta, max_cols: int) -> list[TableMeta]:
    if len(table.columns) <= max_cols:
        return [table]
    return [
        table.model_copy(update={"columns": table.columns[i:i + max_cols]})
        for i in range(0, len(table.columns), max_cols)
    ]
