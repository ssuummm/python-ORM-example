"""LLM 없이 파싱·후처리 로직을 점검하는 테스트.  실행: python -m pytest -q  (또는 python tests/test_pipeline.py)"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.classifier import SensitiveTableClassifier  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.loader import load_keywords, load_tables  # noqa: E402
from src.preprocess import KeywordMatcher  # noqa: E402

S = ROOT / "sample_data"


class ReplayClient(LLMClient):
    """미리 준비한 응답을 순서대로 돌려주는 가짜 LLM."""
    name = "replay"

    def __init__(self, settings, responses):
        super().__init__(settings)
        self.responses = list(responses)
        self.calls = 0

    def complete(self, system, user, context=None):
        self.calls += 1
        return self.responses.pop(0)


def _setup(table_name, responses):
    s = load_settings(provider="mock")
    gk = load_keywords(S / "global_keywords.csv", "global")
    sk = load_keywords(S / "system_keywords.csv", "system")
    t = next(x for x in load_tables(S / "tables.json") if x.table_name == table_name)
    c = ReplayClient(s, responses)
    return SensitiveTableClassifier(gk, sk, s, c), t, c


def test_row_condition_and_guards():
    resp = {
        "decision": "sensitive",
        "sensitive_data": [
            {"type": "row_condition", "column_name": "SEC_GRADE_CD", "row_condition": "SEC_GRADE_CD IN ('S','A')",
             "matched_keyword_ids": ["G-008"], "evidence": ["std_term_desc", "value"], "reason": "극비/대외비 행", "confidence": 0.9},
            {"type": "row_condition", "column_name": "PJT_TYPE_CD", "row_condition": "PJT_TYPE_CD = 'NPX'",
             "matched_keyword_ids": ["S-PMS-001"], "evidence": ["value"], "reason": "신제품 과제", "confidence": 0.8},
            {"type": "column", "column_name": "SECRET_MEMO", "matched_keyword_ids": ["G-008"], "reason": "환각 컬럼", "confidence": 0.9},
        ],
        "reason": "보안등급 S/A 및 신제품 과제 행이 민감", "confidence": 0.88,
    }
    clf, t, _ = _setup("TB_PROJECT_MASTER", ["```json\n" + json.dumps(resp, ensure_ascii=False) + "\n```"])
    r = clf.classify(t)
    assert r.decision == "sensitive" and r.sensitivity_level == "HIGH"
    assert [f.column_name for f in r.sensitive_data] == ["SEC_GRADE_CD", "PJT_TYPE_CD"]   # 환각 컬럼 제거
    assert any("SECRET_MEMO" in w for w in r.validation_warnings)
    assert any("'NPX'" in x for x in r.review_reasons)                                     # 근거 없는 조건 값
    assert not any("SEC_GRADE_CD" in x and "근거" in x for x in r.review_reasons)          # 'S','A'는 근거 있음


def test_retry_on_bad_json_and_legacy_format():
    legacy = json.dumps({"decision": "Sensitive", "sensitive data": ["YLD_RT", "CUST_CD"],
                         "reason": "수율, 고객사", "confidence": "0.9"}, ensure_ascii=False)
    clf, t, c = _setup("TB_LOT_YIELD", ["죄송합니다, 다음과 같습니다", legacy])
    r = clf.classify(t)
    assert c.calls == 2 and r.meta["retries"] == 1
    assert {f.column_name for f in r.sensitive_data} == {"YLD_RT", "CUST_CD"}
    assert r.needs_review   # keyword_id가 없는 초기 포맷 → 검토 대상


def test_missed_high_keyword_and_decision_fix():
    resp = {"decision": "non-sensitive", "sensitive_data": [
        {"type": "column", "column_name": "pwd_hash", "matched_keyword_ids": ["G-003"], "reason": "해시", "confidence": 0.95}],
        "non_sensitive_candidates": [{"column_name": "PWD_CHG_DT", "matched_keyword_ids": ["G-003"], "reason": "변경일자"}],
        "reason": "x", "confidence": 0.9}
    clf, t, _ = _setup("TB_USER_ACCOUNT", [json.dumps(resp)])
    r = clf.classify(t)
    assert r.decision == "sensitive"                                   # 정합성 보정
    assert r.sensitive_data[0].column_name == "PWD_HASH"               # 대소문자 정규화
    # PWD_*는 모두 언급됨 / USER_NM·EMAIL_ADDR은 MEDIUM 키워드라 누락 경고 대상 아님
    assert not any("누락" in x for x in r.review_reasons)


def test_omitted_high_keyword_column_flagged():
    resp = {"decision": "sensitive", "sensitive_data": [
        {"type": "column", "column_name": "SAL_AMT", "matched_keyword_ids": ["G-002"], "reason": "급여", "confidence": 0.95}],
        "reason": "x", "confidence": 0.95}
    clf, t, _ = _setup("TB_EMP_SALARY", [json.dumps(resp)])
    r = clf.classify(t)
    assert any(x.startswith("RRN_ENC") and "누락" in x for x in r.review_reasons)


def test_short_alias_token_match():
    gk = load_keywords(S / "global_keywords.csv", "global")
    t = next(x for x in load_tables(S / "tables.json") if x.table_name == "TB_EQP_MASTER")
    hits = KeywordMatcher(gk).find_hits(t)
    # 'SAL'(3자) 같은 약어는 토큰 단위로만 매칭되어야 함 → EQP_MODEL_NM 등 오탐 없음
    assert all(h.keyword_id != "G-002" for h in hits)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
