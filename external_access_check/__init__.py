"""sql_rewriter — Agent SQL 권한 적용 라이브러리.

Agent 가 생성한 Oracle SQL 을 DB 도달 직전에 자동 재작성해, 사용자 권한 (CLS + RLS)
에 맞는 데이터만 반환하도록 만든다.

기본 사용:

    from sql_rewriter import RewriterRequest, rewrite_sql_request

    # OPA 내부에서 user/context/sql 기반 policy/messages 생성
    response = rewrite_sql_request(
        RewriterRequest(
            user_id=user_id,
            sql=agent_sql,
            policy=opa_policy,
            permission_messages=permission_messages,
        )
    )
    safe_sql = response.rewritten_sql

설계 상세는 docs/TABLE_SUBSTITUTION_PROPOSAL.md.
호출측이 의존할 4가지 보장 (G1 가용성 / G2 CLS NULL / G3 누출 0 / G4 RLS always-on)
은 README.md 와 CLAUDE.md 참조.
"""

from .errors import (
    FullyDeniedError,
    ParseError,
    PolicyMissingError,
    RewriterError,
    UnsupportedSqlError,
)
from .policy import Policy, PolicyParseError, RowFilter, TablePolicy
from .references import (
    AmbiguousReference,
    ProjectionStarReference,
    ReferenceExtractionError,
    ReferenceExtractionResult,
    SqlReferences,
    UnresolvedReference,
)
from .rewriter_service import (
    PermissionMessage,
    RewriterRequest,
    RewriterResponse,
    rewrite_sql_request,
)
from .rewriter import (
    analyze_references,
    referenced_tables,
    rewrite,
    try_analyze_references,
)

__all__ = [
    # public functions
    "rewrite",
    "analyze_references",
    "try_analyze_references",
    "referenced_tables",
    "rewrite_sql_request",
    # types
    "AmbiguousReference",
    "PermissionMessage",
    "Policy",
    "ProjectionStarReference",
    "ReferenceExtractionError",
    "ReferenceExtractionResult",
    "RewriterRequest",
    "RewriterResponse",
    "SqlReferences",
    "TablePolicy",
    "RowFilter",
    "UnresolvedReference",
    # exceptions
    "RewriterError",
    "ParseError",
    "UnsupportedSqlError",
    "PolicyMissingError",
    "FullyDeniedError",
    "PolicyParseError",
]
