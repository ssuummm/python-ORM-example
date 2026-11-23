"""2단계 고위험 유형 태깅: 민감 테이블의 고위험 유형(태그) 매핑.

민감 판정·검증이 끝난 테이블에 대해, 판정 근거(민감 컬럼·행 조건·매칭 키워드·판정 이유)를
LLM에 한 번 더 보내 어떤 고위험 유형에 해당하는지 고른다. 유형은 여러 개 고를 수 있다.
"""
from __future__ import annotations

import json
import logging
from itertools import chain

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .llm_client import LLMClient
from .models import HighRiskTag, Keyword, TableMeta, TableResult
from .parser import extract_json
from .prompt import PROMPT_DIR

log = logging.getLogger(__name__)

TAG_PROMPT_FILE = PROMPT_DIR / "tag_prompt.md"

# 고위험 유형은 아래 5개로 고정한다. LLM 응답의 tag가 이 중 하나가 아니면 버린다.
# (유형을 바꿀 때는 이 목록과 tag_prompt.md의 '고위험 유형 정의'를 함께 고친다.)
TAG_NAMES = ("재무성과", "영업기밀", "사업계획", "기술노하우", "전략제품")


class TagAnswer(BaseModel):
    """태그 LLM의 응답 형식."""
    high_risk_tags: list[HighRiskTag] = Field(default_factory=list)


def build_tag_input(table: TableMeta, result: TableResult, kw_index: dict[str, Keyword]) -> dict:
    """태그 LLM에 보낼 입력: 테이블 정보 + 민감 판정 근거."""
    column_desc = {c.column_name: c.column_desc for c in table.columns}
    findings = result.sensitive_data
    keyword_ids = {m.keyword_id for m in chain.from_iterable(f.matched_keywords for f in findings)}
    return {
        "table_name": table.table_name,
        "table_kor_name": table.table_kor_name,
        "table_desc": table.table_desc,
        "sensitive_columns": [
            {"name": f.column_name, "desc": column_desc.get(f.column_name, ""), "reason": f.reason}
            for f in findings if f.type == "column"
        ],
        "sensitive_row_conditions": [
            {"condition": f.row_condition, "column_desc": column_desc.get(f.column_name, ""), "reason": f.reason}
            for f in findings if f.type == "row_condition"
        ],
        "matched_keywords": [
            {"keyword": kw_index[k].keyword, "description": kw_index[k].description}
            for k in sorted(keyword_ids) if k in kw_index
        ],
        "decision_reason": result.reason,
    }


class HighRiskTagger:
    def __init__(self, keywords: list[Keyword], settings: Settings, client: LLMClient):
        self.kw_index = {k.keyword_id: k for k in keywords}   # 매칭 키워드의 설명을 찾을 때 사용
        self.s = settings
        self.client = client
        self.system_prompt = TAG_PROMPT_FILE.read_text(encoding="utf-8")

    def tag_result(self, table: TableMeta, result: TableResult) -> TableResult:
        """민감 테이블의 결과에 고위험 유형 태그를 붙인다.

        태그가 없거나 LLM 호출이 실패해도 error로 만들지 않고 검토 필요(needs_review)로 둔다.
        """
        try:
            result.high_risk_tags = self.tag(table, result, self.kw_index)
            if not result.high_risk_tags:
                result.review_reasons.append("고위험 유형 미매핑")
        except Exception as e:
            log.warning("태그 매핑 실패: %s (%s)", table.table_id, e)
            result.review_reasons.append(f"고위험 유형 태그 매핑 실패: {e}")
        result.needs_review = bool(result.review_reasons)
        return result

    def tag(self, table: TableMeta, result: TableResult, kw_index: dict[str, Keyword]) -> list[HighRiskTag]:
        """LLM으로 고위험 유형을 고르고, 정해진 유형명만 남겨 돌려준다. 실패하면 ValueError."""
        user = json.dumps(build_tag_input(table, result, kw_index), ensure_ascii=False, indent=1)
        answer, last_err = None, ""
        for attempt in range(self.s.max_retries + 1):          # 형식 오류 시 재요청
            raw = self.client.complete(self.system_prompt, user, {"task": "tag", "table": table})
            try:
                answer = TagAnswer.model_validate(extract_json(raw))
                break                                           # 성공하면 반복 종료
            except (ValueError, ValidationError) as e:
                last_err = str(e).replace("\n", " ")[:300]
                log.warning("태그 응답 파싱 실패(%d회차) %s: %s", attempt + 1, table.table_id, last_err)
        if answer is None:
            raise ValueError(f"태그 응답 파싱 실패: {last_err}")

        tags = {}
        for t in answer.high_risk_tags:
            if t.tag in TAG_NAMES and t.tag not in tags:   # 정해진 5개 유형만, 중복은 처음 것만
                tags[t.tag] = t
        return list(tags.values())
