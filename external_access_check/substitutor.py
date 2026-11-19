"""
substitutor.py

v4 — CLS NULL 보장을 ① 단계에서 구조적으로 달성.

  단계 0  Pre-walk
        트리 전체 scope walk → 각 컬럼 참조를 scope chain 으로 따라가 그 컬럼을
        궁극적으로 노출하는 물리 Table 노드를 찾음 (A.* / * 확장 포함).
        해당 테이블의 all_columns 에 있고 allowed_columns 에 없으면
        (Table 노드, 컬럼명) 으로 등록.

  단계 0b SetOp star padding
        UNION/INTERSECT/EXCEPT branch 의 SELECT * / A.* 가 원래 펼쳤을 hidden
        컬럼은 NULL AS 로 보강 대상에 등록. SetOp 은 컬럼 위치와 개수가
        계약이므로 CLS 이후에도 branch arity 를 보존.

  단계 1  테이블 치환 (보안 본체)
        FROM <물리테이블> <alias>
           →
        FROM (SELECT <all_columns 순서의 allowed + 필요한 banned NULL>
              FROM <물리테이블> WHERE <RLS>) <alias>
        - allowed_columns 의 실제 데이터 + outer 가 참조한 banned 컬럼은 NULL 상수
        - outer 가 banned 를 어떤 위치/표현식에서 참조해도 ORA-00904 가 발생하지
          않음 (G1/G2 보장).

  단계 2  IS NULL 술어 보정 (post-① 트리)
        `banned IS [NOT] NULL` 패턴은 NULL 상수에 대해 의미가 뒤집힘.
        해당 Is 노드 전체를 Oracle-portable false predicate(1 = 0) 로 치환.
        그 외 위치의 banned 참조는 SQL 3치 논리가 자동 처리.

  추가 — Unresolved 참조 fallback
        derived scope 에 노출되지 않은 컬럼 참조 (사용자 SQL alias 누락 등) 는
        실행 시 ORA-00904 가 됨. G1 (가용성) 유지를 위해 NULL 로 치환.
        보안 측면 영향 없음 (banned 는 ① 이 이미 처리).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .names import table_source_name
from .errors import (
    ColumnPolicyMissingError,
    FullyDeniedError,
    ParseError,
    PolicyMissingError,
    UnsupportedSqlError,
)
from .policy import Policy, PolicyParseError, TablePolicy

__all__ = ["substitute", "referenced_physical_tables"]


# =============================================================================
# Public API
# =============================================================================

def substitute(sql: str, policy: Policy, *, dialect: str = "oracle") -> str:
    tree = _parse(sql, dialect)
    _reject_non_select(tree)
    cte_names = _collect_cte_names(tree)

    # 단계 0 — pre-walk: 외부 참조된 banned 컬럼을 Table 노드별로 수집
    banned_per_table = _collect_banned_refs(tree, policy, cte_names)
    _merge_banned_refs(
        banned_per_table,
        _collect_setop_star_padding(tree, policy, cte_names),
    )

    # 단계 1 — 테이블 치환
    #
    # 중요: 최종 SQL 은 tree.sql(...) 로 전체 재출력하지 않는다. sqlglot 의
    # dialect serializer 는 SUBSTRING -> SUBSTR, LIMIT -> FETCH FIRST 같은
    # 원본 SQL 정규화를 수행하기 때문이다. AST 는 분석용으로만 쓰고, 출력은
    # 원본 문자열에 필요한 구간만 splice 한다.
    edits: List[SourceEdit] = []
    for table in list(tree.find_all(exp.Table)):
        if _is_cte_ref(table, cte_names) or _is_dual(table):
            continue
        source_name = table_source_name(table)
        tp = policy.for_table(source_name)
        if tp is None:
            raise PolicyMissingError(source_name)
        # 26.08.14 주석 처리
        # if tp.fully_denied:
        #     raise FullyDeniedError(source_name)
        banned = banned_per_table.get(id(table), set())
        edits.append(_table_replacement_edit(sql, table, tp, banned))

    _check_unregistered_columns(tree, policy, cte_names)
    # 단계 2 — IS NULL 보정 + unresolved fallback
    #
    # 기존 구현은 table 치환 후 AST 를 다시 훑었지만, 그 방식은 최종 tree.sql()
    # 재출력을 요구한다. 여기서는 원본 AST 기준으로 "치환 후 null_constant 가 될
    # 컬럼" 과 unresolved 컬럼만 source span 으로 보정한다.
    edits.extend(_source_preserving_sanitize_edits(sql, tree, policy, cte_names))

    return _apply_source_edits(sql, edits)


def referenced_physical_tables(sql: str, *, dialect: str = "oracle") -> List[str]:
    tree = _parse(sql, dialect)
    cte_names = _collect_cte_names(tree)
    seen: Set[str] = set()
    ordered: List[str] = []
    for table in tree.find_all(exp.Table):
        if _is_cte_ref(table, cte_names) or _is_dual(table):
            continue
        name = table_source_name(table)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


# =============================================================================
# Parsing / structural checks
# =============================================================================

def _parse(sql: str, dialect: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:
        raise ParseError(f"Failed to parse SQL: {e}") from e
    if tree is None:
        raise ParseError("Empty SQL.")
    return tree


def _reject_non_select(tree: exp.Expression) -> None:
    root = tree.this if isinstance(tree, exp.With) else tree
    if isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return
    raise UnsupportedSqlError(
        f"Only SELECT statements are supported; got {type(root).__name__}"
    )


def _collect_cte_names(tree: exp.Expression) -> Set[str]:
    return {cte.alias.upper() for cte in tree.find_all(exp.CTE) if cte.alias}


def _is_cte_ref(table: exp.Table, cte_names: Set[str]) -> bool:
    if table.args.get("db") or table.args.get("catalog"):
        return False
    return table.name.upper() in cte_names


def _is_dual(table: exp.Table) -> bool:
    return (
        not table.args.get("db")
        and not table.args.get("catalog")
        and table.name.upper() == "DUAL"
    )


# =============================================================================
# 단계 0 — Pre-walk: banned 참조 수집 (Table 노드별)
# =============================================================================
#
# 원리: 모든 컬럼 참조를 scope chain 으로 따라가 그 컬럼이 궁극적으로 노출되는
# 물리 Table 노드를 찾는다. derived subquery 의 `A.*` / `*` 확장도 따라간다.
# 노출 테이블의 all_columns 에 있고 allowed_columns 에 없으면 banned 참조로 등록.

BannedRefs = Dict[int, Set[str]]


@dataclass(frozen=True)
class SourceEdit:
    start: int
    end: int
    replacement: str
    reason: str = ""


def _apply_source_edits(sql: str, edits: Iterable[SourceEdit]) -> str:
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
    filtered: List[SourceEdit] = []
    last_end = -1
    for edit in ordered:
        if edit.start < 0 or edit.end < edit.start or edit.end > len(sql):
            raise ParseError(f"invalid source edit span: {edit}")
        if edit.start < last_end:
            # Prefer the wider earlier edit. This mainly protects predicate-level
            # replacements from also applying a nested column replacement.
            continue
        filtered.append(edit)
        last_end = edit.end

    out = sql
    for edit in reversed(filtered):
        out = out[:edit.start] + edit.replacement + out[edit.end:]
    return out


def _identifier_span(identifier: Optional[exp.Expression]) -> Optional[Tuple[int, int]]:
    if identifier is None:
        return None
    meta = getattr(identifier, "meta", {}) or {}
    start = meta.get("start")
    end = meta.get("end")
    if start is None or end is None:
        return None
    return int(start), int(end) + 1


def _table_name_span(table: exp.Table) -> Tuple[int, int]:
    spans = []
    for key in ("catalog", "db", "this"):
        span = _identifier_span(table.args.get(key))
        if span is not None:
            spans.append(span)
    if not spans:
        raise ParseError(f"cannot locate table source span: {table}")
    return min(start for start, _ in spans), max(end for _, end in spans)


def _column_source_span(col: exp.Column) -> Optional[Tuple[int, int]]:
    spans = []
    for key in ("catalog", "db", "table", "this"):
        span = _identifier_span(col.args.get(key))
        if span is not None:
            spans.append(span)
    if not spans:
        return None
    return min(start for start, _ in spans), max(end for _, end in spans)


def _table_replacement_edit(
    sql: str,
    table: exp.Table,
    tp: TablePolicy,
    banned_referenced: Set[str],
) -> SourceEdit:
    start, end = _table_name_span(table)
    source_text = sql[start:end]
    replacement = _secured_subquery_sql(table, source_text, tp, banned_referenced)
    return SourceEdit(start=start, end=end, replacement=replacement, reason="table")


def _secured_subquery_sql(
    table: exp.Table,
    source_text: str,
    tp: TablePolicy,
    banned_referenced: Set[str],
) -> str:
    banned_needed = {c.upper() for c in banned_referenced}
    projections: List[str] = []
    for col in tp.all_columns:
        if tp.is_allowed(col):
            projections.append(col)
        elif col.upper() in banned_needed:
            projections.append(f"NULL AS {col}")

    # 26.08.18 추가
    if not projections:
        # allowed_columns == [] (fully denied) 이면서 outer 가 이 테이블을
        # 이름 있는 컬럼 참조 없이 (예: bare `SELECT *`) 접근한 경우.
        # projections 가 비면 `SELECT FROM t` 형태의 문법 오류가 되므로,
        # all_columns 전체를 NULL AS 로 채워 유효한 SQL을 보장한다.
        projections = [f"NULL AS {col}" for col in tp.all_columns]

    inner = f"(SELECT {', '.join(projections)} FROM {source_text}"
    if tp.row_filter:
        inner += f" WHERE {tp.row_filter}"
    inner += ")"

    if table.args.get("alias") is None:
        inner += f" {table.name}"
    return inner


def _merge_banned_refs(target: BannedRefs, extra: BannedRefs) -> None:
    for table_id, cols in extra.items():
        target.setdefault(table_id, set()).update(cols)


def _collect_banned_refs(
    tree: exp.Expression, policy: Policy, cte_names: Set[str]
) -> BannedRefs:
    """tree 의 모든 Column 참조에 대해 즉시 enclosing scope 를 찾아 분석.

    sqlglot 의 `scope.columns` 는 nested subquery 의 column 도 일부 포함하므로
    여기서는 모든 Column 노드를 직접 순회하고 find_ancestor 로 immediate scope
    를 찾아 그 scope 의 sources 기준으로 해석한다.
    """
    banned: BannedRefs = {}
    root_scope = build_scope(tree)
    if root_scope is None:
        return banned

    scope_by_expr: Dict[int, Scope] = {
        id(scope.expression): scope for scope in root_scope.traverse()
    }

    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star) or col.is_star:
            continue
        owner_expr = col.find_ancestor(
            exp.Select, exp.Union, exp.Intersect, exp.Except
        )
        if owner_expr is None:
            continue
        scope = scope_by_expr.get(id(owner_expr))
        if scope is None:
            continue
        tables = _resolve_column_to_tables(col, scope, cte_names, policy)
        if not tables:
            continue
        # 한 source 라도 col 을 explicitly 노출하면 ref 가 그 쪽으로 unambiguously
        # 해석되므로 banned 등록 안 함 (Oracle 의 unqualified resolution 의미와 정합).
        allowed_anywhere = False
        banned_candidates: List[exp.Table] = []
        for table in tables:
            tp = policy.for_table(table_source_name(table))
            if tp is None:
                continue
            if tp.is_allowed(col.name):
                allowed_anywhere = True
                break
            if tp.is_banned(col.name):
                banned_candidates.append(table)
        if allowed_anywhere:
            continue
        for table in banned_candidates:
            banned.setdefault(id(table), set()).add(col.name.upper())
    return banned


def _resolve_column_to_tables(
    col: exp.Column, scope: Scope, cte_names: Set[str], policy: Policy
) -> List[exp.Table]:
    """col 이 ultimately 노출될 수 있는 물리 Table 노드 후보들을 반환.

    비한정 참조는 어느 한 source 가 col 을 explicitly (all_columns 또는
    명시 projection) 노출하면 그 source 로 우선 해석. 어디에도 explicit 이
    없을 때만 star/uncertain 후보로 fallback 한다 — Oracle 의 unqualified
    column resolution 의미와 일치.

    한정 참조는 immediate scope 에서 못 찾으면 parent scope 까지 거슬러
    올라간다 — 상관 서브쿼리의 outer alias 한정 참조 처리에 필수.
    """
    if col.table:
        current: Optional[Scope] = scope
        while current is not None:
            src = current.sources.get(col.table)
            if src is not None:
                return _resolve_via_source(col.name, src, cte_names, policy)
            current = current.parent
        return []
    # 비한정 — explicit 우선
    has_explicit = any(
        _source_has_explicit(col.name, src, cte_names, policy)
        for src in scope.sources.values()
    )
    results: List[exp.Table] = []
    for src in scope.sources.values():
        if has_explicit and not _source_has_explicit(col.name, src, cte_names, policy):
            continue
        results.extend(_resolve_via_source(col.name, src, cte_names, policy))
    return results


def _source_has_explicit(
    col_name: str, src, cte_names: Set[str], policy: Policy
) -> bool:
    """source 가 col 을 명시적으로 노출하는가 (all_columns 멤버 / 명시 projection)."""
    upper = col_name.upper()
    if isinstance(src, exp.Table):
        if src.name.upper() in cte_names or src.name.upper() == "DUAL":
            return False
        tp = policy.for_table(table_source_name(src))
        if tp is None:
            return False
        return tp.has_column(col_name)
    if isinstance(src, Scope):
        expr = src.expression
        if isinstance(expr, exp.Select):
            for proj in expr.expressions:
                if _projection_name(proj) == upper:
                    return True
        return False
    return False


def _resolve_via_source(
    col_name: str, src, cte_names: Set[str], policy: Policy
) -> List[exp.Table]:
    if isinstance(src, exp.Table):
        if src.name.upper() in cte_names or src.name.upper() == "DUAL":
            return []
        tp = policy.for_table(table_source_name(src))
        if tp is None or not tp.has_column(col_name):
            return []
        return [src]
    if isinstance(src, Scope):
        return _resolve_via_scope_projection(col_name, src, cte_names, policy)
    return []


def _resolve_via_scope_projection(
    col_name: str, scope: Scope, cte_names: Set[str], policy: Policy
) -> List[exp.Table]:
    """derived scope 의 projection 을 통과해 물리 Table 후보로 내려감."""
    expr = scope.expression
    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        # SetOp: 좌측 SELECT 만 따라감 (우측은 sqlglot 이 별도 scope 로 추적)
        if isinstance(expr.left, exp.Select):
            return _resolve_select_projection(
                col_name, expr.left, scope, cte_names, policy
            )
        return []
    if not isinstance(expr, exp.Select):
        return []
    return _resolve_select_projection(col_name, expr, scope, cte_names, policy)


def _resolve_select_projection(
    col_name: str,
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
    policy: Policy,
) -> List[exp.Table]:
    """projection 들을 훑어 col_name 을 노출하는 물리 Table 후보를 찾는다.

    명시 projection 매칭이 있으면 그 결과만 반환 (Oracle 도 explicit 을 우선
    선택). 명시 매칭이 없을 때만 *  / A.* 확장 후보를 검사.
    """
    upper = col_name.upper()
    explicit_match = False
    explicit_results: List[exp.Table] = []

    # Pass 1: 명시 projection
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            continue
        proj_name = _projection_name(proj)
        if proj_name is None or proj_name != upper:
            continue
        explicit_match = True
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column):
            if inner.table:
                src = scope.sources.get(inner.table)
                if src is not None:
                    explicit_results.extend(
                        _resolve_via_source(inner.name, src, cte_names, policy)
                    )
            else:
                # 비한정 inner — recursive resolution within sub-scope
                sub_results = _resolve_unqualified_in_scope(
                    inner.name, scope, cte_names, policy
                )
                explicit_results.extend(sub_results)
        # 그 외 (NVL, CASE 등 표현식 projection) — 이름이 매칭돼도 더 추적 안 함

    if explicit_match:
        return explicit_results

    # Pass 2: star 확장
    results: List[exp.Table] = []
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in scope.sources.values():
                results.extend(_resolve_via_source(col_name, src, cte_names, policy))
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = scope.sources.get(proj.table)
            if src is not None:
                results.extend(_resolve_via_source(col_name, src, cte_names, policy))
            continue
    return results


def _resolve_unqualified_in_scope(
    col_name: str, scope: Scope, cte_names: Set[str], policy: Policy
) -> List[exp.Table]:
    """scope 안의 비한정 col 을 explicit 우선 규칙으로 해석."""
    has_explicit = any(
        _source_has_explicit(col_name, src, cte_names, policy)
        for src in scope.sources.values()
    )
    results: List[exp.Table] = []
    for src in scope.sources.values():
        if has_explicit and not _source_has_explicit(col_name, src, cte_names, policy):
            continue
        results.extend(_resolve_via_source(col_name, src, cte_names, policy))
    return results


def _projection_name(proj: exp.Expression) -> Optional[str]:
    if isinstance(proj, exp.Alias):
        return proj.alias.upper()
    if isinstance(proj, exp.Column):
        return proj.name.upper()
    return None


# =============================================================================
# 단계 0b — SetOp star padding
# =============================================================================
#
# UNION/INTERSECT/EXCEPT 는 branch 별 output arity 가 같아야 한다. 원본이
# `SELECT *` 로 같은 물리 arity 를 갖고 있어도, CLS 적용 후 한쪽의 hidden 컬럼이
# 빠지면 arity 가 깨질 수 있다. SetOp branch 의 star 가 원래 펼쳤을 hidden 컬럼은
# 해당 inner subquery 에 NULL 상수로 보강해 원본 star arity 를 보존한다.


def _collect_setop_star_padding(
    tree: exp.Expression, policy: Policy, cte_names: Set[str]
) -> BannedRefs:
    padding: BannedRefs = {}
    root_scope = build_scope(tree)
    if root_scope is None:
        return padding

    scope_by_expr: Dict[int, Scope] = {
        id(scope.expression): scope for scope in root_scope.traverse()
    }

    seen_selects: Set[int] = set()
    for setop in tree.find_all(exp.Union, exp.Intersect, exp.Except):
        for select_expr in _setop_leaf_selects(setop):
            if id(select_expr) in seen_selects:
                continue
            seen_selects.add(id(select_expr))
            scope = scope_by_expr.get(id(select_expr))
            if scope is None:
                continue
            _collect_star_padding_in_select(
                select_expr, scope, policy, cte_names, padding
            )
    return padding


def _setop_leaf_selects(expr: exp.Expression) -> Iterable[exp.Select]:
    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        left = expr.this
        right = expr.expression
        if left is not None:
            yield from _setop_leaf_selects(left)
        if right is not None:
            yield from _setop_leaf_selects(right)
        return
    if isinstance(expr, exp.Select):
        yield expr


def _collect_star_padding_in_select(
    select_expr: exp.Select,
    scope: Scope,
    policy: Policy,
    cte_names: Set[str],
    padding: BannedRefs,
) -> None:
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in scope.sources.values():
                _add_hidden_columns_from_star_source(src, policy, cte_names, padding)
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = scope.sources.get(proj.table)
            if src is not None:
                _add_hidden_columns_from_star_source(src, policy, cte_names, padding)


def _add_hidden_columns_from_star_source(
    src,
    policy: Policy,
    cte_names: Set[str],
    padding: BannedRefs,
) -> None:
    if isinstance(src, exp.Table):
        if src.name.upper() in cte_names or src.name.upper() == "DUAL":
            return
        tp = policy.for_table(table_source_name(src))
        if tp is None:
            return
        for col in tp.all_columns:
            if tp.is_banned(col):
                padding.setdefault(id(src), set()).add(col)
        return

    if isinstance(src, Scope):
        expr = src.expression
        if not isinstance(expr, exp.Select):
            return
        _collect_star_padding_in_select(expr, src, policy, cte_names, padding)


# =============================================================================
# 단계 1 — Secured subquery 조립
# =============================================================================

def _build_secured_subquery(
    table: exp.Table, tp: TablePolicy, banned_referenced: Set[str], dialect: str
) -> exp.Subquery:
    inner_from = table.copy()
    inner_from.set("alias", None)

    # SELECT * / SetOp 는 컬럼 이름이 아니라 위치도 의미가 있으므로 물리 테이블의
    # all_columns 순서대로 real 또는 NULL projection 을 조립한다.
    banned_needed = {c.upper() for c in banned_referenced}
    projections: List[exp.Expression] = []
    for col in tp.all_columns:
        if tp.is_allowed(col):
            projections.append(exp.column(col))
        elif col.upper() in banned_needed:
            projections.append(exp.alias_(exp.Null(), col))

    inner = exp.Select(expressions=projections).from_(inner_from)

    rls = _build_row_filter(tp, dialect)
    if rls is not None:
        inner = inner.where(rls)

    alias = _preserve_alias(table)
    return exp.Subquery(this=inner, alias=alias)


def _build_row_filter(tp: TablePolicy, dialect: str) -> Optional[exp.Expression]:
    if not tp.row_filters:
        return None
    conditions: List[exp.Expression] = []
    for rf in tp.row_filters:
        if rf.is_raw_expression:
            conditions.append(_parse_raw_filter(rf.expression_sql, dialect))
            continue
        col = exp.column(rf.column)
        values = list(rf.allowed_values)
        if len(values) == 1:
            conditions.append(
                exp.EQ(this=col, expression=exp.Literal.string(values[0]))
            )
        else:
            conditions.append(
                exp.In(
                    this=col,
                    expressions=[exp.Literal.string(v) for v in values],
                )
            )
    return _and_all(conditions)


def _parse_raw_filter(raw_sql: str, dialect: str) -> exp.Expression:
    try:
        expressions = sqlglot.parse(raw_sql, read=dialect)
    except Exception as e:
        raise PolicyParseError(f"failed to parse OPA filter {raw_sql!r}: {e}") from e

    parsed = [expr for expr in expressions if expr is not None]
    if len(parsed) != 1:
        raise PolicyParseError(
            f"OPA filter must contain exactly one predicate: {raw_sql!r}"
        )
    expr = parsed[0]
    if isinstance(expr, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise PolicyParseError(f"OPA filter must be a predicate: {raw_sql!r}")
    return expr


def _and_all(conds: Iterable[exp.Expression]) -> exp.Expression:
    it = iter(conds)
    result = next(it)
    for c in it:
        result = exp.and_(result, c)
    return result


def _preserve_alias(table: exp.Table) -> exp.TableAlias:
    existing = table.args.get("alias")
    if existing is not None:
        return existing.copy()
    return exp.TableAlias(this=exp.to_identifier(table.name))


# =============================================================================
# 단계 2 — Post-substitution sanitize (IS NULL 보정 + unresolved fallback)
# =============================================================================
#
# 원리: ① 이후 트리에서 각 컬럼 참조를 다시 분류한다.
#   - 'null_constant' : substitution 의 `NULL AS X` projection 으로 매핑
#   - 'real'          : substitution 의 실 컬럼으로 매핑 (또는 CTE 같은 외부 source)
#   - 'unresolved'    : 어떤 source 도 노출하지 않음 (사용자 SQL alias 누락 등)
#
# 처리:
#   - IS NULL/IS NOT NULL 안의 col 이 'null_constant' 또는 'unresolved' →
#     Is 노드 전체를 1 = 0 (NULL IS NULL = TRUE 가 되는 의도 위반 차단)
#   - 그 외 위치의 'unresolved' → NULL 리터럴 (G1 가용성 유지)
#   - 'real' 은 손대지 않음
#
# 'null_constant' 의 일반 위치는 SQL 3치 논리로 자동 처리되므로 별도 손대지 않음.

_REAL = "real"
_NULL_CONST = "null_constant"
_UNRESOLVED = "unresolved"


def _source_preserving_sanitize_edits(
    sql: str,
    tree: exp.Expression,
    policy: Policy,
    cte_names: Set[str],
) -> List[SourceEdit]:
    root_scope = build_scope(tree)
    if root_scope is None:
        return []

    scope_by_expr: Dict[int, Scope] = {
        id(scope.expression): scope for scope in root_scope.traverse()
    }

    edits: List[SourceEdit] = []
    for col in list(tree.find_all(exp.Column)):
        if isinstance(col.this, exp.Star) or col.is_star:
            continue
        owner_expr = col.find_ancestor(
            exp.Select, exp.Union, exp.Intersect, exp.Except
        )
        if owner_expr is None:
            continue
        if (
            isinstance(owner_expr, exp.Select)
            and _is_order_by_output_alias(col, owner_expr)
        ):
            continue
        scope = scope_by_expr.get(id(owner_expr))
        if scope is None:
            continue

        kind = _classify_original_column(col, scope, cte_names, policy)
        if kind == _REAL:
            continue
        if kind == _UNRESOLVED and _is_quoted_column(col):
            continue

        parent = col.parent
        if isinstance(parent, exp.Is):
            edit = _is_null_predicate_edit(sql, col)
            if edit is not None:
                edits.append(edit)
            continue
        if (
            isinstance(parent, exp.Is)
            and isinstance(parent.parent, exp.Not)
            and parent.parent.this is parent
        ):
            edit = _is_null_predicate_edit(sql, col)
            if edit is not None:
                edits.append(edit)
            continue
        if kind == _UNRESOLVED:
            edit = _unresolved_column_edit(sql, col)
            if edit is not None:
                edits.append(edit)

    return edits


def _classify_original_column(
    col: exp.Column,
    scope: Scope,
    cte_names: Set[str],
    policy: Policy,
) -> str:
    if col.table:
        current: Optional[Scope] = scope
        while current is not None:
            src = current.sources.get(col.table)
            if src is not None:
                return _classify_original_via_source(
                    col.name,
                    src,
                    cte_names,
                    policy,
                )
            current = current.parent
        return _UNRESOLVED

    has_real = False
    has_null = False
    if not scope.sources:
        return _UNRESOLVED
    for src in scope.sources.values():
        kind = _classify_original_via_source(col.name, src, cte_names, policy)
        if kind == _REAL:
            has_real = True
        elif kind == _NULL_CONST:
            has_null = True
    if has_real:
        return _REAL
    if has_null:
        return _NULL_CONST
    return _UNRESOLVED


def _classify_original_via_source(
    col_name: str,
    src,
    cte_names: Set[str],
    policy: Policy,
) -> str:
    if isinstance(src, exp.Table):
        if src.name.upper() in cte_names:
            return _REAL
        if src.name.upper() == "DUAL":
            return _UNRESOLVED
        tp = policy.for_table(table_source_name(src))
        if tp is None:
            return _UNRESOLVED
        if tp.is_allowed(col_name):
            return _REAL
        if tp.is_banned(col_name):
            return _NULL_CONST
        return _UNRESOLVED

    if isinstance(src, Scope):
        return _classify_original_via_scope_projection(
            col_name,
            src,
            cte_names,
            policy,
        )
    return _UNRESOLVED


def _classify_original_via_scope_projection(
    col_name: str,
    scope: Scope,
    cte_names: Set[str],
    policy: Policy,
) -> str:
    expr = scope.expression
    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        if isinstance(expr.left, exp.Select):
            return _classify_original_in_select(
                col_name,
                expr.left,
                scope,
                cte_names,
                policy,
            )
        return _UNRESOLVED
    if not isinstance(expr, exp.Select):
        return _UNRESOLVED
    return _classify_original_in_select(col_name, expr, scope, cte_names, policy)


def _classify_original_in_select(
    col_name: str,
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
    policy: Policy,
) -> str:
    upper = col_name.upper()
    has_real = False
    has_null = False

    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in scope.sources.values():
                kind = _classify_original_via_source(
                    col_name,
                    src,
                    cte_names,
                    policy,
                )
                if kind == _REAL:
                    has_real = True
                elif kind == _NULL_CONST:
                    has_null = True
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = scope.sources.get(proj.table)
            if src is not None:
                kind = _classify_original_via_source(
                    col_name,
                    src,
                    cte_names,
                    policy,
                )
                if kind == _REAL:
                    has_real = True
                elif kind == _NULL_CONST:
                    has_null = True
            continue

        proj_name = _projection_name(proj)
        if proj_name is None or proj_name != upper:
            continue

        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Column):
            if inner.table:
                current: Optional[Scope] = scope
                while current is not None:
                    src = current.sources.get(inner.table)
                    if src is not None:
                        kind = _classify_original_via_source(
                            inner.name,
                            src,
                            cte_names,
                            policy,
                        )
                        if kind == _REAL:
                            has_real = True
                        elif kind == _NULL_CONST:
                            has_null = True
                        break
                    current = current.parent
            else:
                kind = _classify_original_column(inner, scope, cte_names, policy)
                if kind == _REAL:
                    has_real = True
                elif kind == _NULL_CONST:
                    has_null = True
            continue

        # Derived expression aliases (ROW_NUMBER() AS rn, COUNT(*) AS c, etc.)
        # are visible real columns from the derived scope's perspective.
        has_real = True

    if has_real:
        return _REAL
    if has_null:
        return _NULL_CONST
    return _UNRESOLVED


def _is_null_predicate_edit(sql: str, col: exp.Column) -> Optional[SourceEdit]:
    span = _column_source_span(col)
    if span is None:
        return None
    start, end = span
    match = re.match(r"\s+IS\s+(?:NOT\s+)?NULL\b", sql[end:], flags=re.IGNORECASE)
    if match is None:
        return None
    return SourceEdit(
        start=start,
        end=end + match.end(),
        replacement="1 = 0",
        reason="is-null",
    )


def _unresolved_column_edit(sql: str, col: exp.Column) -> Optional[SourceEdit]:
    span = _column_source_span(col)
    if span is None:
        return None
    start, end = span
    parent = col.parent
    if col.arg_key == "expressions" and isinstance(parent, exp.Select):
        return SourceEdit(
            start=start,
            end=end,
            replacement=f"NULL AS {col.name}",
            reason="unresolved-projection",
        )
    return SourceEdit(start=start, end=end, replacement="NULL", reason="unresolved")


def _post_substitute_sanitize(tree: exp.Expression, cte_names: Set[str]) -> None:
    """post-① 트리의 컬럼 참조를 분류해 IS NULL 보정 + unresolved fallback 수행.

    sqlglot 의 `scope.columns` 는 nested subquery 의 column 도 일부 포함하므로
    여기서는 모든 Column 노드를 직접 순회하고 각 컬럼의 즉시 enclosing scope
    (Select/Union/Intersect/Except) 를 find_ancestor 로 찾아 그 scope 에 대해
    분류한다.
    """
    root_scope = build_scope(tree)
    if root_scope is None:
        return

    scope_by_expr: Dict[int, Scope] = {
        id(scope.expression): scope for scope in root_scope.traverse()
    }

    for col in list(tree.find_all(exp.Column)):
        # Star (A.* / *) 는 컬럼 참조가 아니라 확장 기호 — 건드리지 않음
        if isinstance(col.this, exp.Star) or col.is_star:
            continue
        owner_expr = col.find_ancestor(
            exp.Select, exp.Union, exp.Intersect, exp.Except
        )
        if owner_expr is None:
            continue
        if (
            isinstance(owner_expr, exp.Select)
            and _is_order_by_output_alias(col, owner_expr)
        ):
            continue
        scope = scope_by_expr.get(id(owner_expr))
        if scope is None:
            continue
        kind = _classify_column(col, scope, cte_names)
        if kind == _REAL:
            continue
        if kind == _UNRESOLVED and _is_quoted_column(col):
            continue
        parent = col.parent
        if isinstance(parent, exp.Is):
            grandparent = parent.parent
            if isinstance(grandparent, exp.Not) and grandparent.this is parent:
                grandparent.replace(_false_predicate())
            else:
                parent.replace(_false_predicate())
            continue
        if kind == _UNRESOLVED:
            _replace_column_with_null(col)


def _classify_column(
    col: exp.Column, scope: Scope, cte_names: Set[str]
) -> str:
    if col.table:
        # 상관 서브쿼리: outer alias 한정 참조는 parent scope 에서 resolve.
        current: Optional[Scope] = scope
        while current is not None:
            src = current.sources.get(col.table)
            if src is not None:
                return _classify_via_source(col.name, src, cte_names)
            current = current.parent
        return _UNRESOLVED

    # 비한정 — 어느 한 source 라도 'real' 이면 real, 모두 'unresolved' 면 unresolved,
    # 'null_constant' 만 있으면 null_constant
    has_real = False
    has_null = False
    if not scope.sources:
        return _UNRESOLVED
    for src in scope.sources.values():
        kind = _classify_via_source(col.name, src, cte_names)
        if kind == _REAL:
            has_real = True
        elif kind == _NULL_CONST:
            has_null = True
    if has_real:
        return _REAL
    if has_null:
        return _NULL_CONST
    return _UNRESOLVED


def _is_order_by_output_alias(col: exp.Column, select_expr: exp.Select) -> bool:
    """ORDER BY 의 SELECT-list alias 참조는 물리 컬럼처럼 sanitize 하지 않는다."""
    if col.table:
        return False
    order_expr = select_expr.args.get("order")
    if order_expr is None or not _is_descendant_of(col, order_expr):
        return False

    name = col.name.upper()
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Alias) and proj.alias.upper() == name:
            return True
    return False


def _is_descendant_of(node: exp.Expression, ancestor: exp.Expression) -> bool:
    current: Optional[exp.Expression] = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _false_predicate() -> exp.Expression:
    """Oracle-compatible predicate that is always false."""
    return exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(0))


def _classify_via_source(col_name: str, src, cte_names: Set[str]) -> str:
    if isinstance(src, exp.Table):
        # ① 이후엔 정책 적용된 물리 Table 은 모두 Subquery 로 치환됨.
        # 여기 남는 것은 CTE / DUAL — 보수적으로 real 로 본다.
        if src.name.upper() in cte_names:
            return _REAL
        if src.name.upper() == "DUAL":
            return _UNRESOLVED  # DUAL 에는 컬럼 없음 (Oracle 의 DUMMY 1개 빼고)
        return _REAL
    if isinstance(src, Scope):
        return _classify_via_scope_projection(col_name, src, cte_names)
    return _UNRESOLVED


def _classify_via_scope_projection(
    col_name: str, scope: Scope, cte_names: Set[str]
) -> str:
    expr = scope.expression
    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        if isinstance(expr.left, exp.Select):
            return _classify_in_select(col_name, expr.left, scope, cte_names)
        return _UNRESOLVED
    if not isinstance(expr, exp.Select):
        return _UNRESOLVED
    return _classify_in_select(col_name, expr, scope, cte_names)


def _classify_in_select(
    col_name: str,
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
) -> str:
    upper = col_name.upper()
    has_real = False
    has_null = False

    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in scope.sources.values():
                k = _classify_via_source(col_name, src, cte_names)
                if k == _REAL:
                    has_real = True
                elif k == _NULL_CONST:
                    has_null = True
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = scope.sources.get(proj.table)
            if src is not None:
                k = _classify_via_source(col_name, src, cte_names)
                if k == _REAL:
                    has_real = True
                elif k == _NULL_CONST:
                    has_null = True
            continue
        proj_name = _projection_name(proj)
        if proj_name is None or proj_name != upper:
            continue
        # 이름 매칭 — alias 가 NULL 리터럴이면 null_constant
        if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Null):
            has_null = True
            continue
        # 그 외 (Column 또는 표현식) → real
        has_real = True

    if has_real:
        return _REAL
    if has_null:
        return _NULL_CONST
    return _UNRESOLVED


def _replace_column_with_null(col: exp.Column) -> None:
    """'unresolved' 컬럼 참조를 NULL 리터럴로 치환 (G1 가용성).

    SELECT projection 에 직접 놓인 케이스는 `NULL AS <원래이름>` 으로 — 결과 컬럼
    이름 계약 보존.
    """
    parent = col.parent
    col_name = col.name
    if col.arg_key == "expressions" and isinstance(parent, exp.Select):
        col.replace(exp.alias_(exp.Null(), col_name))
        return
    col.replace(exp.Null())


def _is_quoted_column(col: exp.Column) -> bool:
    return bool(getattr(col.this, "quoted", False))


# 26.11.05 추가 / 26.11.15 개정 — 신규(미등록) 컬럼 체크
#
# [판정 원칙]
#   원본 SQL 이 Oracle 에서 유효하다면, 참조된 컬럼은 보이는 테이블 중 어딘가에 반드시 있다.
#   그러므로 "보이는 테이블 전부에 그 컬럼이 없다"고 확신할 수 있을 때만 에러를 낸다.
#   조금이라도 확신할 수 없으면 에러를 내지 않는다 (오탐 방지).
#
# [용어]
#   source : FROM / JOIN 에 나오는 테이블, 인라인 뷰, CTE, LATERAL 서브쿼리
#   scope  : 하나의 SELECT 블록 (sqlglot 이 만들어 주는 단위)
#
# [판정 결과는 세 가지]
#   _YES     : source 가 그 컬럼을 가진다 (또는 가질 수 있다)
#   _NO      : source 가 그 컬럼을 갖지 않는다고 확정
#   _UNKNOWN : 알 수 없다

_YES = "YES"
_NO = "NO"
_UNKNOWN = "UNKNOWN"

# 실제 컬럼명이 될 수 없는 의사 컬럼 — e.ROWID 처럼 한정해도 항상 제외
_ALWAYS_PSEUDO_COLUMNS = {"ROWNUM", "ROWID", "LEVEL", "ORA_ROWSCN"}

# 괄호 없이 쓰는 내장 함수 — 한정자 없이 쓸 때만 제외
_NO_PAREN_BUILTINS = {
    "USER", "UID", "SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "SESSIONTIMEZONE", "DBTIMEZONE",
    "ORA_INVOKING_USER", "ORA_INVOKING_USERID",
}

# CONNECT BY 가 있는 SELECT 블록에서만 의사 컬럼
_CONNECT_BY_PSEUDO_COLUMNS = {"CONNECT_BY_ISLEAF", "CONNECT_BY_ISCYCLE"}

# VERSIONS / AS OF 절이 붙은 테이블이 있는 SELECT 블록에서만 의사 컬럼
_VERSIONS_PSEUDO_COLUMNS = {
    "VERSIONS_STARTTIME", "VERSIONS_ENDTIME", "VERSIONS_STARTSCN",
    "VERSIONS_ENDSCN", "VERSIONS_XID", "VERSIONS_OPERATION",
}

# 괄호 없이 호출하는 사용자 함수 (신규 컬럼과 구분할 수 없음) — 대문자로 등록
_NO_PAREN_FUNCTION_ALLOWLIST: Set[str] = set()

# 참고: OBJECT_ID / OBJECT_VALUE / COLUMN_VALUE / NEXTVAL / CURRVAL 은 일반 컬럼처럼
# 검사한다 (정책 대상에 object table 이 없다는 전제). 전제가 깨지면
# OBJECT_ID, OBJECT_VALUE 를 _ALWAYS_PSEUDO_COLUMNS 에 추가한다.

_SET_OPS = (exp.Union, exp.Intersect, exp.Except)

# 인라인 뷰 → 인라인 뷰 → ... 를 따라 내려가는 최대 깊이 (무한 재귀 방지)
_MAX_DEPTH = 30


def _check_unregistered_columns(
    tree: exp.Expression, policy: Policy, cte_names: Set[str]
) -> None:
    """보이는 source 어디에도 없다고 확정되는 컬럼이 있으면 ColumnPolicyMissingError.

    - 한정 참조 (E.COL)  : alias 가 가리키는 source 로 판정 (대소문자 무시)
    - 비한정 참조 (COL)  : FROM/JOIN 의 모든 source 에 없고, 상관 서브쿼리라면
                           바깥 쿼리 source 에도 없을 때만 에러
    - USING 절, PIVOT/UNPIVOT 절 안의 컬럼도 검사
    - 의사 컬럼, 괄호 없는 내장 함수, ORDER BY / GROUP BY alias 는 제외
    """
    checker = _UnregisteredColumnChecker(tree, policy, cte_names)
    checker.run()


class _UnregisteredColumnChecker:
    """한 SQL 에 대한 검사 1회분. 같은 계산을 반복하지 않도록 결과를 저장해 둔다."""

    def __init__(self, tree: exp.Expression, policy: Policy, cte_names: Set[str]):
        self.tree = tree
        self.policy = policy
        self.cte_names = cte_names

        # SELECT 노드 → scope
        self.scope_of: Dict[int, Scope] = {}
        root_scope = build_scope(tree)
        if root_scope is not None:
            for scope in root_scope.traverse():
                self.scope_of[id(scope.expression)] = scope

        # 캐시 (key 는 id(...) — 같은 노드면 같은 결과)
        self._sources_cache: Dict[int, Optional[list]] = {}
        self._names_cache: Dict[int, Optional[dict]] = {}
        self._has_pivot_cache: Dict[int, bool] = {}
        self._flashback_cache: Dict[int, bool] = {}
        self._source_has_cache: Dict[Tuple[str, int, int], str] = {}

    # =========================================================================
    # 1. 진입점
    # =========================================================================

    def run(self) -> None:
        if not self.scope_of:
            return
        for col in self.tree.find_all(exp.Column):
            self._check_column(col)
        for join in self.tree.find_all(exp.Join):
            self._check_using(join)

    def _check_column(self, col: exp.Column) -> None:
        # (1) 컬럼이 아닌 것은 건너뛴다
        if col.is_star or isinstance(col.this, exp.Star):
            return
        if _is_quoted_column(col):
            return
        if isinstance(col.parent, exp.Partition):          # PARTITION (P2024)
            return
        if col.find_ancestor(exp.MatchRecognize) is not None:  # 패턴 변수 A.COL
            return

        # (2) 이 컬럼이 속한 SELECT 블록을 찾는다
        owner = col.find_ancestor(exp.Select, *_SET_OPS)
        if not isinstance(owner, exp.Select):
            return      # SetOp 전체의 ORDER BY → 첫 branch 의 출력 이름만 참조 가능
        scope = self.scope_of.get(id(owner))
        if scope is None:
            return

        # (3) 의사 컬럼 / 괄호 없는 함수
        if self._is_not_a_column(col, owner):
            return

        # (4) PIVOT / UNPIVOT 절 안의 컬럼
        pivot = col.find_ancestor(exp.Pivot)
        if pivot is not None and not _is_descendant_of(owner, pivot):
            self._check_pivot_column(col, pivot)
            return

        # (5) PIVOT 결과를 참조하는 컬럼은 출력 컬럼을 알 수 없다
        if self._scope_has_pivot(scope):
            return

        # (6) SELECT 목록의 alias 를 ORDER BY / GROUP BY 에서 참조한 것
        if _is_order_by_output_alias(col, owner) or _is_group_by_alias(col, owner):
            return

        # (7) 실제 검사
        if col.table:
            self._check_qualified(col, scope)
        else:
            self._check_unqualified(col, scope)

    # =========================================================================
    # 2. 검사 종류별 로직
    # =========================================================================

    def _check_qualified(self, col: exp.Column, scope: Scope) -> None:
        """E.COL — E 가 가리키는 source 하나만 본다."""
        # E.DOC.city 같은 JSON dot / 객체 속성 표기는 검사하지 않는다
        for part in ("db", "catalog"):
            ident = col.args.get(part)
            if ident is not None and self._find_source(ident.name, scope) is not None:
                return

        found = self._find_source(col.table, scope)
        if not found:
            return      # 시퀀스, 패키지 상수, PIVOT alias 등 source 가 아닌 한정자
        node, src = found
        if self._source_has(col.name, node, src) == _NO:
            self._raise(col.name, [src])

    def _check_unqualified(self, col: exp.Column, scope: Scope) -> None:
        """COL — FROM/JOIN 의 모든 source 와 (상관 서브쿼리면) 바깥 source 를 본다."""
        sources = self._sources(scope)
        if not sources:
            return
        for node, src in sources:
            if self._source_has(col.name, node, src) != _NO:
                return  # 한 곳이라도 가질 수 있으면 통과
        if self._visible_in_outer_query(col.name, scope):
            return
        self._raise(col.name, [src for _, src in sources])

    def _check_pivot_column(self, col: exp.Column, pivot: exp.Pivot) -> None:
        """PIVOT 의 집계 인자·FOR 컬럼, UNPIVOT 의 IN 목록 컬럼은 원본 source 의 컬럼이다."""
        if col.table:
            return
        holder = pivot.parent      # PIVOT 이 붙은 테이블 또는 서브쿼리
        if holder is None or len(holder.args.get("pivots") or []) != 1:
            return

        # PIVOT 이면 IN 목록 밖, UNPIVOT 이면 IN 목록 안의 컬럼만 원본 컬럼이다
        is_unpivot = bool(pivot.args.get("unpivot"))
        if _is_in_value_list(col, pivot) != is_unpivot:
            return

        if isinstance(holder, exp.Table):
            result = self._table_has(col.name, holder)
            sources = [holder]
        elif isinstance(holder, exp.Subquery):
            inner_scope = self.scope_of.get(id(holder.this))
            if inner_scope is None:
                return
            result = self._scope_has(col.name, inner_scope, 0)
            sources = [inner_scope]
        else:
            return

        if result == _NO:
            self._raise(col.name, sources)

    def _check_using(self, join: exp.Join) -> None:
        """JOIN ... USING (COL) — COL 은 오른쪽 source 와 왼쪽 source 양쪽에 있어야 한다."""
        using = join.args.get("using")
        select = join.parent
        if not using or not isinstance(select, exp.Select):
            return
        scope = self.scope_of.get(id(select))
        if scope is None or self._scope_has_pivot(scope):
            return
        sources = self._sources(scope)
        if not sources:
            return

        # 이 JOIN 과 그 뒤의 JOIN 에 나오는 노드 = 아직 조인되지 않은 쪽
        later_nodes = set()
        reached = False
        for other in select.args.get("joins") or []:
            if other is join:
                reached = True
            if reached and other.this is not None:
                later_nodes.add(id(other.this))
                later_nodes.add(id(other.this.unnest()))

        right = [(n, s) for n, s in sources if _is_join_item(n, join.this)]
        left = [(n, s) for n, s in sources if id(n) not in later_nodes]
        if len(right) != 1 or not left:
            return
        right_node, right_src = right[0]

        for ident in using:
            name = ident.name
            if not name or getattr(ident, "quoted", False):
                continue
            if self._source_has(name, right_node, right_src) == _NO:
                self._raise(name, [right_src])
            if all(self._source_has(name, n, s) == _NO for n, s in left):
                self._raise(name, [s for _, s in left])

    # =========================================================================
    # 3. 제외 규칙
    # =========================================================================

    def _is_not_a_column(self, col: exp.Column, owner: exp.Select) -> bool:
        """의사 컬럼이나 괄호 없는 함수처럼 컬럼이 아닌 이름인가. ("..." 는 항상 컬럼)"""
        name = col.name.upper()
        if name in _ALWAYS_PSEUDO_COLUMNS:
            return True
        if col.table:
            return False
        if name in _NO_PAREN_BUILTINS or name in _NO_PAREN_FUNCTION_ALLOWLIST:
            return True
        if name in _CONNECT_BY_PSEUDO_COLUMNS:
            return owner.args.get("connect") is not None
        if name in _VERSIONS_PSEUDO_COLUMNS:
            return self._has_flashback_table(owner)
        return False

    def _has_flashback_table(self, owner: exp.Select) -> bool:
        """이 SELECT 블록의 테이블 중 VERSIONS / AS OF 절이 붙은 것이 있는가."""
        key = id(owner)
        if key not in self._flashback_cache:
            self._flashback_cache[key] = any(
                table.args.get("version") is not None
                for table in owner.find_all(exp.Table)
                if table.find_ancestor(exp.Select) is owner
            )
        return self._flashback_cache[key]

    def _scope_has_pivot(self, scope: Scope) -> bool:
        """이 SELECT 블록의 FROM 에 PIVOT / UNPIVOT 이 붙어 있는가."""
        key = id(scope)
        if key not in self._has_pivot_cache:
            expr = scope.expression
            self._has_pivot_cache[key] = isinstance(expr, exp.Select) and any(
                pivot.find_ancestor(exp.Select) is expr
                for pivot in expr.find_all(exp.Pivot)
            )
        return self._has_pivot_cache[key]

    # =========================================================================
    # 4. source 목록 / 이름 찾기
    # =========================================================================

    def _sources(self, scope: Scope) -> Optional[list]:
        """이 SELECT 블록의 FROM/JOIN 에 실제로 나열된 source 의 [(node, source)].

        scope.sources 는 '보이기만 하는' CTE 까지 포함하므로 쓰지 않는다.
        목록을 확신할 수 없으면 None 을 돌려준다 (호출측은 검사하지 않는다).
        """
        key = id(scope)
        if key not in self._sources_cache:
            self._sources_cache[key] = _list_selected_sources(scope)
        return self._sources_cache[key]

    def _sources_by_name(self, scope: Scope) -> Optional[dict]:
        """{대문자 alias: (node, source)}. 목록을 만들 수 없으면 None."""
        key = id(scope)
        if key not in self._names_cache:
            try:
                selected = scope.selected_sources   # alias 가 중복되면 예외
            except Exception:
                self._names_cache[key] = None
            else:
                by_name: dict = {}
                for name, pair in selected.items():
                    by_name.setdefault(str(name).upper(), pair)
                self._names_cache[key] = by_name
        return self._names_cache[key]

    def _find_source(self, qualifier: str, scope: Scope):
        """한정자(alias/테이블명)가 가리키는 (node, source) 를 안쪽 블록부터 찾는다.

        못 찾으면 None, 중간 블록의 source 목록을 확신할 수 없으면 False.
        """
        wanted = qualifier.upper()
        current: Optional[Scope] = scope
        while current is not None:
            by_name = self._sources_by_name(current)
            if by_name is None:
                return False
            if wanted in by_name:
                return by_name[wanted]
            current = current.parent
        return None

    def _visible_in_outer_query(self, col_name: str, scope: Scope) -> bool:
        """상관 참조로 바깥 쿼리의 컬럼일 가능성이 있는가.

        WHERE/SELECT 안의 서브쿼리와 LATERAL 은 자신을 품은 블록의 source 를 볼 수 있다.
        SetOp branch · 인라인 뷰 · CTE 는 바로 바깥 블록은 못 보지만, 그 블록이
        상관 서브쿼리 안에 있으면 더 바깥은 볼 수 있으므로 한 단계 올라가기만 한다.
        """
        child = scope
        while child.parent is not None:
            parent = child.parent
            kind = str(getattr(child.scope_type, "name", child.scope_type)).upper()
            if kind in ("SUBQUERY", "UDTF"):
                sources = self._sources(parent)
                if sources is None:
                    return True
                for node, src in sources:
                    if src is not child and self._source_has(col_name, node, src) != _NO:
                        return True
            child = parent
        return False

    # =========================================================================
    # 5. source 가 컬럼을 갖는가 → _YES / _NO / _UNKNOWN
    # =========================================================================

    def _source_has(self, col_name: str, node, src, depth: int = 0) -> str:
        key = (col_name.upper(), id(node), id(src))
        if key not in self._source_has_cache:
            self._source_has_cache[key] = self._compute_source_has(
                col_name, node, src, depth
            )
        return self._source_has_cache[key]

    def _compute_source_has(self, col_name: str, node, src, depth: int) -> str:
        if depth > _MAX_DEPTH:
            return _UNKNOWN
        # PIVOT / UNPIVOT 이 붙은 source 는 출력 컬럼을 알 수 없다
        for holder in (node, getattr(node, "parent", None)):
            if isinstance(holder, (exp.Table, exp.Subquery)) and holder.args.get("pivots"):
                return _UNKNOWN
        if isinstance(src, exp.Table):
            return self._table_has(col_name, src)
        if isinstance(src, Scope):
            return self._scope_has(col_name, src, depth)
        return _UNKNOWN

    def _table_has(self, col_name: str, table: exp.Table) -> str:
        """물리 테이블 → 정책의 all_columns 로 판정."""
        if not isinstance(table.this, exp.Identifier):
            return _UNKNOWN                     # TABLE(...), JSON_TABLE, XMLTABLE
        if not (table.args.get("db") or table.args.get("catalog")):
            name = table.name.upper()
            if name == "DUAL":
                return _UNKNOWN
            if name in self.cte_names:
                # scope 로 풀리지 않은 CTE 이름 = 재귀 CTE 의 자기참조
                columns = _recursive_cte_columns(table, name)
                if columns and col_name.upper() in columns:
                    return _YES
                return _UNKNOWN
        tp = self.policy.for_table(table_source_name(table))
        if tp is None:
            return _UNKNOWN
        return _YES if tp.has_column(col_name) else _NO

    def _scope_has(self, col_name: str, scope: Scope, depth: int) -> str:
        """인라인 뷰 / CTE / LATERAL → SELECT 목록과 그 아래 source 로 판정."""
        if depth > _MAX_DEPTH:
            return _UNKNOWN
        upper = col_name.upper()
        expr = scope.expression

        # LATERAL / CROSS APPLY → 안쪽 SELECT 로 판정
        if isinstance(expr, exp.Lateral):
            inner = expr.this.unnest() if expr.this is not None else None
            inner_scope = self.scope_of.get(id(inner)) if inner is not None else None
            if inner_scope is None:
                return _UNKNOWN                 # JSON_TABLE, TABLE(...) 등
            return self._scope_has(col_name, inner_scope, depth + 1)

        holder = expr.parent                    # CTE 또는 Subquery 노드
        alias_columns = _alias_column_names(holder)

        # UNION 등 → 컬럼 이름은 맨 왼쪽 SELECT 가 정한다
        select_scope = scope
        while isinstance(expr, _SET_OPS):
            expr = expr.this.unnest() if expr.this is not None else None
            select_scope = self.scope_of.get(id(expr)) if expr is not None else None
            if select_scope is None:
                return _UNKNOWN
        if not isinstance(expr, exp.Select):
            return _UNKNOWN

        has_unnamed_expression = False
        if alias_columns is not None:
            # WITH c(a, b) AS (...) → 컬럼 이름은 a, b
            if upper in alias_columns:
                return _YES
            # SEARCH / CYCLE 절은 alias 목록에 없는 컬럼을 덧붙인다
            with_clause = holder.parent if isinstance(holder, exp.CTE) else None
            if with_clause is not None and (
                with_clause.args.get("search") or with_clause.args.get("cycle")
            ):
                return _UNKNOWN
        else:
            for projection in expr.expressions:
                if isinstance(projection, exp.Star):
                    continue
                if isinstance(projection, exp.Column) and projection.is_star:
                    continue
                name = _projection_name(projection)
                if name is None:
                    has_unnamed_expression = True   # COUNT(*) 처럼 이름이 없는 식
                elif name == upper:
                    return _YES

        # SELECT 목록에 없으면 아래 source 로 판정한다.
        # (* 가 없어도 아래 source 가 가지면 '가질 수 있음'으로 둔다 — 오탐 방지)
        sources = self._sources(select_scope)
        if sources is None:
            return _UNKNOWN
        result = _NO
        for node, src in sources:
            answer = self._source_has(col_name, node, src, depth + 1)
            if answer == _YES:
                return _YES
            if answer == _UNKNOWN:
                result = _UNKNOWN
        if result == _NO and has_unnamed_expression:
            return _UNKNOWN
        return result

    # =========================================================================
    # 6. 에러
    # =========================================================================

    def _raise(self, col_name: str, sources: list) -> None:
        """후보 테이블이 여러 개면 'EMP, DEPT' 처럼 이어 붙여 전달한다."""
        tables: List[str] = []
        for src in sources:
            if isinstance(src, exp.Table):
                candidates = [src]
            elif isinstance(src, Scope):
                candidates = list(src.expression.find_all(exp.Table))
            else:
                candidates = []
            for table in candidates:
                if not isinstance(table.this, exp.Identifier):
                    continue
                tp = self.policy.for_table(table_source_name(table))
                if tp is not None and tp.table not in tables:
                    tables.append(tp.table)
        raise ColumnPolicyMissingError(", ".join(tables), col_name.upper())


# =============================================================================
# 상태가 필요 없는 작은 헬퍼들
# =============================================================================

def _list_selected_sources(scope: Scope) -> Optional[list]:
    """scope 의 FROM/JOIN source 목록. 확신할 수 없으면 None."""
    expr = scope.expression
    if not isinstance(expr, exp.Select):
        return []
    try:
        selected = scope.selected_sources       # alias 가 중복되면 예외
        references = scope.references
    except Exception:
        return None

    from_clause = expr.args.get("from_") or expr.args.get("from")
    item_count = (1 if from_clause is not None else 0) + len(expr.args.get("joins") or [])
    if len(selected) != len(references) or len(selected) < item_count:
        return None                             # alias 없는 인라인 뷰 2개 등
    if len({str(name).upper() for name in selected}) != len(selected):
        return None                             # 대소문자만 다른 alias
    return list(selected.values())


def _is_group_by_alias(col: exp.Column, select: exp.Select) -> bool:
    """GROUP BY / HAVING 에서 SELECT 목록의 alias 를 참조했는가 (Oracle 23ai)."""
    if col.table:
        return False
    name = col.name.upper()
    is_alias = any(
        isinstance(p, exp.Alias) and p.alias.upper() == name
        for p in select.expressions
    )
    if not is_alias:
        return False
    for key in ("group", "having"):
        clause = select.args.get(key)
        if clause is not None and _is_descendant_of(col, clause):
            return True
    return False


def _is_in_value_list(col: exp.Column, pivot: exp.Pivot) -> bool:
    """컬럼이 PIVOT/UNPIVOT 의 IN ( ... ) 목록 안에 있는가."""
    node: Optional[exp.Expression] = col
    while node is not None and node is not pivot:
        if node.arg_key == "expressions" and isinstance(node.parent, exp.In):
            return True
        node = node.parent
    return False


def _is_join_item(node: exp.Expression, item: Optional[exp.Expression]) -> bool:
    """node 가 JOIN 오른쪽에 나온 그 테이블/서브쿼리인가."""
    return item is not None and (node is item or node is item.unnest())


def _alias_column_names(holder: Optional[exp.Expression]) -> Optional[Set[str]]:
    """WITH c(x, y) AS (...) 처럼 컬럼명을 재지정했으면 {"X", "Y"}, 아니면 None."""
    if holder is None:
        return None
    alias = holder.args.get("alias")
    columns = alias.args.get("columns") if alias is not None else None
    if not columns:
        return None
    return {c.name.upper() for c in columns}


def _recursive_cte_columns(table: exp.Table, name: str) -> Optional[Set[str]]:
    """재귀 CTE 안에서 자기 자신을 참조할 때, 그 CTE 의 alias 컬럼 목록."""
    cte = table.find_ancestor(exp.CTE)
    while cte is not None:
        if cte.alias.upper() == name:
            return _alias_column_names(cte)
        cte = cte.find_ancestor(exp.CTE)
    return None
