"""
rewriter.py

공개 API. 호출측은 이 모듈만 import 하면 된다.

OPA 는 SQL/user/context 로 권한을 판단한 뒤 rewriter_service.py 의 request
handler 또는 이 모듈의 in-process API 로 policy 를 적용한다. Reference
extraction 은 권한 메시지 품질을 높이는 보조 입력일 뿐 별도 Parser 단계가 아니다.

    from sql_rewriter import rewrite, try_analyze_references, Policy

    extraction = try_analyze_references(agent_sql)
    opa_json = opa_client.evaluate(user_context, extraction.to_opa_input())
    policy = Policy.from_opa_response(opa_json)
    rewritten = rewrite(agent_sql, policy)
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from .policy import Policy, PolicyParseError, RowFilter, TablePolicy
from .references import (
    ReferenceExtractionResult,
    SqlReferences,
    analyze_sql_references,
    try_analyze_sql_references,
)
from .substitutor import referenced_physical_tables, substitute
from .errors import (
    FullyDeniedError,
    ParseError,
    PolicyMissingError,
    RewriterError,
    UnsupportedSqlError,
)

__all__ = [
    # public function
    "rewrite",
    "analyze_references",
    "try_analyze_references",
    "referenced_tables",
    # types
    "Policy",
    "TablePolicy",
    "RowFilter",
    "SqlReferences",
    "ReferenceExtractionResult",
    # exceptions
    "RewriterError",
    "ParseError",
    "UnsupportedSqlError",
    "PolicyMissingError",
    "FullyDeniedError",
    "PolicyParseError",
]


def rewrite(sql: str, policy: Policy, *, dialect: str = "oracle") -> str:
    """Agent SQL 에 권한 정책을 적용한 SQL 을 반환.

    Raises:
        ParseError: SQL 파싱 실패
        UnsupportedSqlError: SELECT 외 구문
        PolicyMissingError: 참조 테이블에 대한 정책 없음
        FullyDeniedError: 테이블 정책은 있으나 allowed_columns 가 공백
    """
    return substitute(sql, policy, dialect=dialect)


def referenced_tables(sql: str, *, dialect: str = "oracle") -> list:
    """SQL 이 실제로 참조하는 물리 테이블 이름 목록 (대문자, 중복 제거).

    OPA 호출 payload 구성에 사용.
    """
    return referenced_physical_tables(sql, dialect=dialect)


def analyze_references(
    sql: str,
    *,
    dialect: str = "oracle",
    table_columns: Optional[Mapping[str, Iterable[str]]] = None,
) -> SqlReferences:
    """OPA 요청 payload 구성을 위한 SQL table/column reference 분석."""
    return analyze_sql_references(
        sql,
        dialect=dialect,
        table_columns=table_columns,
    )


def try_analyze_references(
    sql: str,
    *,
    dialect: str = "oracle",
    table_columns: Optional[Mapping[str, Iterable[str]]] = None,
) -> ReferenceExtractionResult:
    """운영 파이프라인용 non-blocking SQL reference 분석."""
    return try_analyze_sql_references(
        sql,
        dialect=dialect,
        table_columns=table_columns,
    )

