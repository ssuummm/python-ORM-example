"""입력 메타데이터 / LLM 출력 / 최종 결과 스키마."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

TABLE_TARGET = "__TABLE__"

# =========================================================
# 입력
# =========================================================
class Keyword(BaseModel):
    keyword_id: str
    keyword: str
    description: str = ""
    equivalent_terms: list[str] = Field(default_factory=list)
    high_risk_tag: str = ""
    sys_cd: str = "*"                                  # "*"이면 모든 시스템에 적용

    @property
    def terms(self) -> list[str]:
        """사전 매칭에 쓰는 표현 목록: keyword + equivalent_terms"""
        return [self.keyword, *self.equivalent_terms]


class ColumnMeta(BaseModel):
    column_name: str
    column_desc: str = ""
    equivalent_columns: str = ""
    std_term: str = ""
    std_term_desc: str = ""
    data_type: str = ""
    sample_values: list[str] = Field(default_factory=list)
    sample_source: str = ""        # keyword | keyword+frequency | frequency

    @field_validator("sample_values", mode="before")
    @classmethod
    def _to_str_list(cls, v):
        if v is None:
            return []
        return [str(x) for x in v if x is not None and str(x) != ""]


class TableMeta(BaseModel):
    sys_cd: str
    schema_name: str = ""
    table_name: str
    table_desc: str = ""
    columns: list[ColumnMeta] = Field(default_factory=list)

    @property
    def table_id(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


class KeywordHit(BaseModel):
    """규칙 기반 사전 매칭 결과 (LLM 힌트 + 후처리 검증용)."""
    keyword_id: str
    term: str
    target: str   # TABLE_TARGET 또는 컬럼명
    field: str    # table_name | table_desc | column_name | column_desc | std_term | std_term_desc | value


# =========================================================
# LLM 원시 출력
# =========================================================
class SensitiveItem(BaseModel):
    type: Literal["column", "row_condition"] = "column"
    column_name: str
    row_condition: str | None = None
    matched_keyword_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CandidateNote(BaseModel):
    column_name: str
    matched_keyword_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class LLMDecision(BaseModel):
    decision: Literal["sensitive", "non-sensitive"]
    sensitive_data: list[SensitiveItem] = Field(default_factory=list)
    non_sensitive_candidates: list[CandidateNote] = Field(default_factory=list)
    unlisted_candidates: list[CandidateNote] = Field(default_factory=list)
    reason: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool = False
    review_reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any):
        """초기 포맷("sensitive data": ["컬럼명", ...]) 등 흔한 변형을 흡수한다."""
        if not isinstance(data, dict):
            return data
        if "sensitive data" in data and "sensitive_data" not in data:
            data["sensitive_data"] = data.pop("sensitive data")
        d = data.get("decision")
        if isinstance(d, str):
            d = d.strip().lower().replace("_", "-").replace(" ", "-")
            data["decision"] = {"nonsensitive": "non-sensitive", "not-sensitive": "non-sensitive"}.get(d, d)
        conf = data.get("confidence", 0.5)
        items = []
        for it in data.get("sensitive_data") or []:
            if isinstance(it, str):
                it = {"type": "column", "column_name": it, "confidence": conf}
            items.append(it)
        data["sensitive_data"] = items
        return data


# =========================================================
# 최종 결과 (코드가 검증·보강한 뒤 저장)
# =========================================================
class MatchedKeywordInfo(BaseModel):
    keyword_id: str
    keyword: str
    sys_cd: str
    high_risk_tag: str = ""


class SensitiveFinding(BaseModel):
    type: Literal["column", "row_condition"]
    column_name: str
    row_condition: str | None = None
    matched_keywords: list[MatchedKeywordInfo] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0


class TableResult(BaseModel):
    table_id: str
    sys_cd: str
    decision: Literal["sensitive", "non-sensitive", "error"]
    sensitive_data: list[SensitiveFinding] = Field(default_factory=list)
    non_sensitive_candidates: list[CandidateNote] = Field(default_factory=list)
    unlisted_candidates: list[CandidateNote] = Field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    prefilter_hit_count: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None