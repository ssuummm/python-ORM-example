"""
rewriter_service.py

OPA -> Rewriter 단계의 얇은 request handler.

OPA 가 이미 권한을 판단하고 user_id/table 별 policy 와 권한 메시지를 넘겨준
상태를 전제로 한다. 이 모듈은 policy 를 파싱한 뒤 SQL rewrite 를 수행하고,
OPA 가 만든 권한 메시지를 그대로 최종 응답에 싣는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .policy import Policy
from .substitutor import substitute

__all__ = [
    "PermissionMessage",
    "RewriterRequest",
    "RewriterResponse",
    "rewrite_sql_request",
]

PermissionMessage = Mapping[str, Any]


@dataclass(frozen=True)
class RewriterRequest:
    """OPA 가 rewriter 에 보내는 요청."""

    user_id: str
    sql: str
    policy: Mapping[str, Any]
    permission_messages: Sequence[PermissionMessage] = field(default_factory=tuple)
    dialect: str = "oracle"


@dataclass(frozen=True)
class RewriterResponse:
    """Rewriter 가 OPA 또는 호출측에 돌려주는 최종 결과."""

    user_id: str
    rewritten_sql: str
    permission_messages: Sequence[PermissionMessage] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "rewritten_sql": self.rewritten_sql,
            "permission_messages": [
                dict(message) for message in self.permission_messages
            ],
        }


def rewrite_sql_request(request: RewriterRequest) -> RewriterResponse:
    """OPA -> Rewriter request 를 처리한다."""

    policy = Policy.from_opa_response(request.policy)
    rewritten_sql = substitute(request.sql, policy, dialect=request.dialect)
    return RewriterResponse(
        user_id=request.user_id,
        rewritten_sql=rewritten_sql,
        permission_messages=tuple(request.permission_messages),
    )

