"""민감 테이블의 고위험 유형(태그) 매핑.

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

# 고위험 유형: 유형명 → 분류 기준.  유형을 바꿀 때는 여기만 고치면 프롬프트와 검증에 함께 반영된다.
TAG_TYPES = {
    "전략제품": (
        "[무엇을 만드나] 전략 제품의 개발·설계·사양·성능·품질 및 고객 인증 정보. "
        "제외: 제조 공정의 수율·공정 조건(→기술노하우), 제품별 생산·판매 계획(→사업계획)"
    ),
    "사업계획": (
        "[앞으로 무엇을 하나] 회사 내부의 미래 시점 수요 예측, 생산·판매·공급·투자 계획과 목표. "
        "제외: 이미 발생한 실적, 특정 고객사와 합의한 거래 물량(→영업기밀)"
    ),
    "영업기밀": (
        "[어느 고객과 어떻게 거래하나] 고객사가 식별되는 거래 가격·계약 조건·공급/출하 물량(계획·실적 모두). "
        "제외: 고객이 식별되지 않는 전체 판매 계획(→사업계획), 원가·이익률(→재무성과)"
    ),
    "재무성과": (
        "[얼마나 남기나] 제품·고객·사업 단위의 원가, 이익, 이익률, 수익성 지표. "
        "제외: 고객에게 제시한 판매 가격 자체(→영업기밀)"
    ),
    "기술노하우": (
        "[어떻게 만드나] 제조 공정의 상세 수율, 레시피·공정 조건·설비 파라미터, 불량 분석, 신뢰성·테스트 평가 결과. "
        "제외: 완제품의 사양·성능(→전략제품)"
    ),
    "개인정보": (
        "[누구에 관한 정보인가] 구성원을 식별할 수 있는 정보와 그 구성원의 평가·보상·건강·징계·연락처 정보. "
        "제외: 개인을 식별할 수 없는 집계 통계"
    ),
}


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
    def __init__(self, client: LLMClient, settings: Settings):
        self.client = client
        self.s = settings
        tag_lines = "\n".join(f"- {name}: {rule}" for name, rule in TAG_TYPES.items())
        self.system_prompt = (PROMPT_DIR / "tag_prompt.md").read_text(encoding="utf-8").replace("{{TAG_TYPES}}", tag_lines)

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
            if t.tag in TAG_TYPES and t.tag not in tags:   # 정해진 유형명만, 중복은 처음 것만
                tags[t.tag] = t
        return list(tags.values())
