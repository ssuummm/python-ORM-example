"""LLM 호출 전 전처리.

1) 규칙 기반 키워드 사전 매칭 → LLM 힌트 + 후처리 교차검증에 사용
2) LLM에 보낼 테이블 JSON 만들기
3) 컬럼이 많은 테이블 분할
"""
from __future__ import annotations

import re
from collections import defaultdict

from .models import TABLE_TARGET, Keyword, KeywordHit, TableMeta

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
# 2. LLM에 보낼 테이블 JSON
# ---------------------------------------------------------------------------
def build_table_payload(table: TableMeta) -> dict:
    """테이블·컬럼 정보를 LLM에 보낼 JSON으로 만든다. 비어 있는 항목은 뺀다."""
    columns = []
    for c in table.columns:
        col = {"name": c.column_name}
        if c.column_desc:
            col["desc"] = c.column_desc
        if c.std_term:
            col["std_term"] = c.std_term
        if c.std_term_desc:
            col["std_term_desc"] = c.std_term_desc
        if c.data_type:
            col["type"] = c.data_type
        columns.append(col)

    return {
        "table_id": table.table_id,
        "system_code": table.system_code,
        "table_name": table.table_name,
        "table_desc": table.table_desc,
        "columns": columns,
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