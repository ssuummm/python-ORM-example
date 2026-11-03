"""LLM 판단 결과의 병합·검증·보강.

LLM 출력을 그대로 믿지 않고 코드로 다음을 강제한다.
- 입력에 없는 컬럼명 / 키워드 ID 제거 (환각 방지)
- row_condition의 SQL 안전성 및 조건 값의 근거(샘플 값·표준용어 설명) 확인
- decision ↔ sensitive_data 정합성 (보안 우선: 항목이 있으면 sensitive)
- 고위험 키워드가 명확히 걸린 컬럼을 LLM이 언급하지 않으면 검토 대상
- 민감도 등급은 LLM이 아니라 매칭된 키워드 등급으로 결정
"""
from __future__ import annotations

import re

from .config import Settings
from .models import (
    TABLE_TARGET, CandidateNote, Keyword, KeywordHit, LLMDecision,
    MatchedKeywordInfo, SensitiveFinding, TableMeta, TableResult,
)

ALLOWED_EVIDENCE = {"table_name", "table_desc", "column_name", "column_desc",
                    "std_term", "std_term_desc", "value", "value_pattern"}
_SQL_FORBIDDEN = re.compile(r";|--|/\*|\b(DROP|DELETE|UPDATE|INSERT|ALTER|UNION|SELECT|EXEC|GRANT)\b", re.I)
_SQL_ALLOWED_CHARS = re.compile(r"^[\w\s'(),.=<>!%가-힣\-]+$")
_QUOTED = re.compile(r"'([^']*)'")


def merge_decisions(ds: list[LLMDecision]) -> LLMDecision:
    if len(ds) == 1:
        return ds[0]
    sens = [d for d in ds if d.decision == "sensitive"]
    return LLMDecision(
        decision="sensitive" if sens else "non-sensitive",
        sensitive_data=[i for d in ds for i in d.sensitive_data],
        non_sensitive_candidates=[i for d in ds for i in d.non_sensitive_candidates],
        unlisted_candidates=[i for d in ds for i in d.unlisted_candidates],
        reason=" / ".join(f"[{n}] {d.reason}" for n, d in enumerate(ds, 1) if d.reason),
        # 민감: 가장 확신 있는 근거 1개로 충분 / 비민감: 가장 약한 묶음이 전체 확신도
        confidence=max(d.confidence for d in sens) if sens else min(d.confidence for d in ds),
        needs_review=any(d.needs_review for d in ds),
        review_reason=" / ".join(d.review_reason for d in ds if d.review_reason),
    )


def _check_row_condition(cond: str, column: str, col_names: set[str],
                         evidence_text: str) -> list[str]:
    errs = []
    if _SQL_FORBIDDEN.search(cond) or not _SQL_ALLOWED_CHARS.match(cond):
        errs.append(f"row_condition 형식 위반: {cond}")
        return errs
    if column.upper() not in cond.upper():
        errs.append(f"row_condition에 대상 컬럼({column}) 없음: {cond}")
    unquoted = _QUOTED.sub("''", cond)
    unknown = [n for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", unquoted)
               if "_" in n and n.upper() not in col_names]
    if unknown:
        errs.append(f"row_condition에 존재하지 않는 컬럼: {unknown}")
    for lit in _QUOTED.findall(cond):
        if lit and lit.upper() not in evidence_text.upper():
            errs.append(f"row_condition 값 '{lit}'의 근거(샘플 값/표준용어 설명)를 찾지 못함")
    return errs


def finalize(table: TableMeta, d: LLMDecision, kw_index: dict[str, Keyword],
             hits: list[KeywordHit], settings: Settings, meta: dict) -> TableResult:
    warnings: list[str] = []
    review: list[str] = []
    cols = {c.column_name.upper(): c for c in table.columns}

    def resolve_col(name: str) -> str | None:
        c = cols.get((name or "").strip().upper())
        return c.column_name if c else None

    def resolve_kws(ids: list[str], where: str) -> list[MatchedKeywordInfo]:
        out = []
        for kid in ids:
            k = kw_index.get(kid)
            if k is None:
                warnings.append(f"{where}: 존재하지 않는 keyword_id '{kid}' 제거")
                continue
            out.append(MatchedKeywordInfo(keyword_id=k.keyword_id, keyword=k.keyword,
                                          sys_cd=k.sys_cd, high_risk_tag=k.high_risk_tag))
        return out

    # --- 민감 항목 검증 ---
    findings: list[SensitiveFinding] = []
    for it in d.sensitive_data:
        col = resolve_col(it.column_name)
        if col is None:
            warnings.append(f"입력에 없는 컬럼 '{it.column_name}' 항목 제거")
            continue
        mk = resolve_kws(it.matched_keyword_ids, col)
        if not mk:
            review.append(f"{col}: 근거 키워드 없이 민감 판정")
        ev = [e for e in it.evidence if e in ALLOWED_EVIDENCE]
        rc = None
        if it.type == "row_condition":
            rc = (it.row_condition or "").strip()
            c = cols[col.upper()]
            errs = _check_row_condition(rc, col, set(cols), " ".join([*c.sample_values, c.std_term_desc, c.column_desc]))
            if not rc:
                errs = ["row_condition 비어 있음"]
            if errs:
                review.extend(f"{col}: {e}" for e in errs)
        findings.append(SensitiveFinding(type=it.type, column_name=col, row_condition=rc,
                                         matched_keywords=mk, evidence=ev,
                                         reason=it.reason, confidence=it.confidence))

    def clean_notes(notes: list[CandidateNote], where: str) -> list[CandidateNote]:
        out = []
        for n in notes:
            col = resolve_col(n.column_name) or (TABLE_TARGET if n.column_name.upper() in ("", TABLE_TARGET, table.table_name.upper()) else None)
            if col is None:
                warnings.append(f"{where}: 입력에 없는 컬럼 '{n.column_name}' 제거")
                continue
            ids = [k.keyword_id for k in resolve_kws(n.matched_keyword_ids, where)]
            out.append(CandidateNote(column_name=col, matched_keyword_ids=ids, reason=n.reason))
        return out

    non_sens = clean_notes(d.non_sensitive_candidates, "non_sensitive_candidates")
    unlisted = clean_notes(d.unlisted_candidates, "unlisted_candidates")
    if unlisted:
        review.append(f"키워드 사전 밖 민감 의심 컬럼 {len(unlisted)}건 (키워드 사전 보강 검토)")

    # --- decision 정합성 ---
    decision = d.decision
    if findings and decision == "non-sensitive":
        warnings.append("민감 항목이 있으나 decision=non-sensitive → sensitive로 보정")
        decision = "sensitive"
    if not findings and decision == "sensitive":
        review.append("decision=sensitive이나 유효한 민감 항목 없음")

    # --- 사전 매칭과 교차검증: 강한 HIGH 매칭 컬럼이 판단에서 누락된 경우 ---
    mentioned = {f.column_name for f in findings} | {n.column_name for n in non_sens}
    strong_fields = {"column_name", "column_desc", "std_term"}
    for h in hits:
        k = kw_index.get(h.keyword_id)
        if (h.target != TABLE_TARGET and h.target not in mentioned and h.field in strong_fields and k is not None):
            review.append(f"{h.target}: HIGH 키워드 {k.keyword_id}({k.keyword}) 매칭되었으나 LLM 판단에서 누락")
            mentioned.add(h.target)

    if d.confidence < settings.review_confidence_threshold:
        review.append(f"confidence {d.confidence:.2f} < 기준 {settings.review_confidence_threshold}")
    if d.needs_review:
        review.append("LLM 요청: " + (d.review_reason or "사유 미기재"))

    return TableResult(
        table_id=table.table_id, sys_cd=table.sys_cd,
        decision=decision, sensitive_data=findings, non_sensitive_candidates=non_sens, 
        unlisted_candidates=unlisted, reason=d.reason, confidence=d.confidence,
        needs_review=bool(review), review_reasons=list(dict.fromkeys(review)),
        validation_warnings=warnings, prefilter_hit_count=len(hits), meta=meta,
    )