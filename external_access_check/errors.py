"""
errors.py

Rewriter 예외 계층. 호출측에서 실패 원인별로 분기할 수 있도록 분리.
"""

from __future__ import annotations

__all__ = [
    "RewriterError",
    "ParseError",
    "UnsupportedSqlError",
    "PolicyMissingError",
    "FullyDeniedError",
    "ColumnPolicyMissingError",
]


class RewriterError(Exception):
    """rewrite 실패의 공통 베이스."""


class ParseError(RewriterError):
    """sqlglot 이 SQL 을 파싱하지 못함."""


class UnsupportedSqlError(RewriterError):
    """SELECT 외 DML (UPDATE/DELETE/INSERT) 이나 미지원 구문."""


class PolicyMissingError(RewriterError):
    """SQL 이 참조하는 테이블에 대한 OPA 정책이 없음.

    보안상 기본은 fail-close — 정책 없는 테이블 접근은 거부.
    """

    def __init__(self, table: str):
        super().__init__(f"No policy registered for table {table!r}")
        self.table = table


class FullyDeniedError(RewriterError):
    """테이블은 있으나 allowed_columns 가 비어 있음 — 전면 거부."""

    def __init__(self, table: str):
        super().__init__(f"Access to table {table!r} is fully denied")
        self.table = table


# 26.11.05 추가
class ColumnPolicyMissingError(RewriterError):
    """SQL 이 참조하는 컬럼이 테이블 정책의 all_columns 에 없음 (신규 컬럼).

    table 은 후보 테이블이 여러 개면 'EMP, DEPT' 처럼 쉼표로 이어진 문자열이다.
    """

    def __init__(self, table: str, column: str):
        super().__init__(
            f"Column {column!r} is not registered in the policy of table {table!r}"
        )
        self.table = table
        self.column = column
