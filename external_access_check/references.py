"""
references.py

OPA 요청 payload 를 만들기 위한 SQL projection extraction.

이 모듈은 보안 집행을 하지 않는다. SQL 이 어떤 물리 테이블을
언급했는지, 최종 projection 이 어떤 물리 컬럼을 노출하려는지, 그리고
schema 없이 해석할 수 없는 projection 참조가 무엇인지만 수집한다.
실제 권한 집행은 OPA 응답을 Policy 로 파싱한 뒤 substitutor.py 의 rewrite
단계가 담당한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .errors import ParseError, UnsupportedSqlError

__all__ = [
    "AmbiguousReference",
    "ProjectionStarReference",
    "ReferenceExtractionError",
    "ReferenceExtractionResult",
    "SqlReferences",
    "UnresolvedReference",
    "analyze_sql_references",
    "try_analyze_sql_references",
]


@dataclass(frozen=True)
class ProjectionStarReference:
    """최종 SELECT projection 의 * / A.* 가 물리 테이블까지 해석된 참조."""

    table: str
    qualifier: Optional[str] = None


@dataclass(frozen=True)
class UnresolvedReference:
    """schema 또는 alias 정보만으로 물리 테이블까지 해석할 수 없는 참조."""

    column: str
    qualifier: Optional[str] = None
    reason: str = "unknown_source"


@dataclass(frozen=True)
class AmbiguousReference:
    """여러 물리 테이블 후보 중 어느 쪽 컬럼인지 확정할 수 없는 참조."""

    column: str
    candidates: Tuple[str, ...]
    qualifier: Optional[str] = None


@dataclass(frozen=True)
class SqlReferences:
    """OPA authorize 요청에 실을 수 있는 SQL projection reference 요약."""

    tables: List[str]
    projected_columns: Dict[str, List[str]]
    projection_stars: List[ProjectionStarReference]
    unresolved: List[UnresolvedReference]
    ambiguous: List[AmbiguousReference]

    @classmethod
    def empty(cls) -> "SqlReferences":
        return cls(
            tables=[],
            projected_columns={},
            projection_stars=[],
            unresolved=[],
            ambiguous=[],
        )

    def to_opa_input(self) -> dict:
        """JSON 직렬화 가능한 dict 로 변환."""
        return {
            "tables": list(self.tables),
            "projected_columns": {
                table: list(cols) for table, cols in self.projected_columns.items()
            },
            "projection_stars": [asdict(star) for star in self.projection_stars],
            "unresolved": [asdict(ref) for ref in self.unresolved],
            "ambiguous": [asdict(ref) for ref in self.ambiguous],
        }


@dataclass(frozen=True)
class ReferenceExtractionError:
    """projection extraction 실패 정보를 OPA/logging payload 에 싣기 위한 타입."""

    type: str
    message: str


@dataclass(frozen=True)
class ReferenceExtractionResult:
    """운영 파이프라인용 non-blocking projection extraction 결과."""

    status: str
    references: SqlReferences
    error: Optional[ReferenceExtractionError] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_opa_input(self) -> dict:
        return {
            "status": self.status,
            "references": self.references.to_opa_input(),
            "error": asdict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class _ResolvedColumn:
    table: str
    column: str


class _Collector:
    def __init__(self) -> None:
        self.tables: List[str] = []
        self._table_seen: Set[str] = set()
        self.projected_columns: Dict[str, List[str]] = {}
        self._projected_column_seen: Dict[str, Set[str]] = {}
        self.projection_stars: List[ProjectionStarReference] = []
        self._projection_star_seen: Set[Tuple[str, Optional[str]]] = set()
        self.unresolved: List[UnresolvedReference] = []
        self._unresolved_seen: Set[Tuple[str, Optional[str], str]] = set()
        self.ambiguous: List[AmbiguousReference] = []
        self._ambiguous_seen: Set[Tuple[str, Optional[str], Tuple[str, ...]]] = set()

    def add_table(self, table: str) -> None:
        table = table.upper()
        if table in self._table_seen:
            return
        self._table_seen.add(table)
        self.tables.append(table)

    def add_projected_column(self, table: str, column: str) -> None:
        table = table.upper()
        column = column.upper()
        self.add_table(table)
        seen = self._projected_column_seen.setdefault(table, set())
        if column in seen:
            return
        seen.add(column)
        self.projected_columns.setdefault(table, []).append(column)

    def add_projection_star(self, table: str, qualifier: Optional[str]) -> None:
        table = table.upper()
        normalized_qualifier = qualifier.upper() if qualifier else None
        self.add_table(table)
        key = (table, normalized_qualifier)
        if key in self._projection_star_seen:
            return
        self._projection_star_seen.add(key)
        self.projection_stars.append(
            ProjectionStarReference(table=table, qualifier=normalized_qualifier)
        )

    def add_unresolved(
        self, column: str, qualifier: Optional[str], reason: str
    ) -> None:
        column = column.upper()
        normalized_qualifier = qualifier.upper() if qualifier else None
        key = (column, normalized_qualifier, reason)
        if key in self._unresolved_seen:
            return
        self._unresolved_seen.add(key)
        self.unresolved.append(
            UnresolvedReference(
                column=column,
                qualifier=normalized_qualifier,
                reason=reason,
            )
        )

    def add_ambiguous(
        self, column: str, qualifier: Optional[str], candidates: Iterable[str]
    ) -> None:
        column = column.upper()
        normalized_qualifier = qualifier.upper() if qualifier else None
        ordered = _dedupe_upper(candidates)
        if not ordered:
            self.add_unresolved(column, normalized_qualifier, "no_candidates")
            return
        key = (column, normalized_qualifier, tuple(ordered))
        if key in self._ambiguous_seen:
            return
        self._ambiguous_seen.add(key)
        self.ambiguous.append(
            AmbiguousReference(
                column=column,
                qualifier=normalized_qualifier,
                candidates=tuple(ordered),
            )
        )

    def finish(self) -> SqlReferences:
        return SqlReferences(
            tables=list(self.tables),
            projected_columns={
                table: list(cols) for table, cols in self.projected_columns.items()
            },
            projection_stars=list(self.projection_stars),
            unresolved=list(self.unresolved),
            ambiguous=list(self.ambiguous),
        )


ReferenceSchema = Optional[Dict[str, Set[str]]]


def analyze_sql_references(
    sql: str,
    *,
    dialect: str = "oracle",
    table_columns: Optional[Mapping[str, Iterable[str]]] = None,
) -> SqlReferences:
    """SQL 이 참조하는 물리 테이블과 projection 컬럼 요약을 반환한다.

    보안 판단에 필요한 최종 권한 정보는 이 결과가 아니라 OPA 응답의
    Policy 계약에서 온다. 이 함수의 출력은 OPA 가 사용자 친화적인 권한
    메시지를 만들고 테이블별 정책을 materialize 하기 위한 입력이다.
    """

    schema = _normalize_table_columns(table_columns)
    tree = _parse(sql, dialect)
    _reject_non_select(tree)
    cte_names = _collect_cte_names(tree)

    collector = _Collector()
    for table in tree.find_all(exp.Table):
        if _is_cte_ref(table, cte_names) or _is_dual(table):
            continue
        collector.add_table(table.name)

    root_scope = build_scope(tree)
    if root_scope is None:
        return collector.finish()

    _collect_root_projection(root_scope, cte_names, collector, schema)

    return collector.finish()


def try_analyze_sql_references(
    sql: str,
    *,
    dialect: str = "oracle",
    table_columns: Optional[Mapping[str, Iterable[str]]] = None,
) -> ReferenceExtractionResult:
    """운영 파이프라인용 non-blocking projection extraction.

    Projection 추출은 OPA 안내 메시지 품질을 높이는 보조 단계다. 실패하더라도
    OPA 호출과 rewrite 권한 집행은 계속되어야 하므로, 이 함수는 예외를
    밖으로 던지지 않고 실패 상태와 빈 references 를 반환한다.
    """
    try:
        refs = analyze_sql_references(
            sql,
            dialect=dialect,
            table_columns=table_columns,
        )
    except Exception as e:
        return ReferenceExtractionResult(
            status="failed",
            references=SqlReferences.empty(),
            error=ReferenceExtractionError(
                type=type(e).__name__,
                message=str(e),
            ),
        )
    return ReferenceExtractionResult(status="ok", references=refs)


def _normalize_table_columns(
    table_columns: Optional[Mapping[str, Iterable[str]]],
) -> ReferenceSchema:
    if table_columns is None:
        return None
    return {
        str(table).upper(): {str(column).upper() for column in columns}
        for table, columns in table_columns.items()
    }


def _schema_has_column(
    schema: ReferenceSchema, table: str, column: str
) -> Optional[bool]:
    if schema is None:
        return None
    columns = schema.get(table.upper())
    if columns is None:
        return None
    return column.upper() in columns


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


def _collect_root_projection(
    root_scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> None:
    expr = root_scope.expression
    if isinstance(expr, exp.Select):
        _collect_select_projection(expr, root_scope, cte_names, collector, schema)
        return

    if not isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        return

    scope_by_expression = {
        id(scope.expression): scope for scope in root_scope.traverse()
    }
    for select in _setop_leaf_selects(expr):
        leaf_scope = scope_by_expression.get(id(select))
        if leaf_scope is not None:
            _collect_select_projection(
                select, leaf_scope, cte_names, collector, schema
            )


def _collect_select_projection(
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> None:
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in _scope_sources(scope).values():
                _collect_projection_star_from_source(
                    src, None, cte_names, collector, schema
                )
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            qualifier = proj.table
            src = _scope_sources(scope).get(qualifier)
            if src is None:
                collector.add_unresolved("*", qualifier, "unknown_qualifier")
                continue
            _collect_projection_star_from_source(
                src, qualifier, cte_names, collector, schema
            )
            continue

        inner = proj.this if isinstance(proj, exp.Alias) else proj
        for item in _collect_columns_in_expression(
            inner, scope, cte_names, collector, schema
        ):
            collector.add_projected_column(item.table, item.column)


def _resolve_column(
    col: exp.Column,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    if col.table:
        src = _scope_sources(scope).get(col.table)
        if src is None:
            collector.add_unresolved(col.name, col.table, "unknown_qualifier")
            return []
        if not _source_exposes_column(col.name, src, cte_names, schema):
            collector.add_unresolved(col.name, col.table, "derived_projection")
            return []
        resolved = _resolve_via_source(col.name, src, cte_names, collector, schema)
        if not resolved and isinstance(src, exp.Table):
            collector.add_unresolved(col.name, col.table, "derived_projection")
        return resolved

    sources = list(_scope_sources(scope).values())
    if len(sources) == 1:
        src = sources[0]
        if not _source_exposes_column(col.name, src, cte_names, schema):
            collector.add_unresolved(col.name, None, "single_source_unresolved")
            return []
        resolved = _resolve_via_source(col.name, src, cte_names, collector, schema)
        if not resolved and isinstance(src, exp.Table):
            collector.add_unresolved(col.name, None, "single_source_unresolved")
        return resolved

    return _resolve_unqualified_multi_source(
        col, sources, cte_names, collector, schema
    )


def _resolve_unqualified_multi_source(
    col: exp.Column,
    sources: List[object],
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    """schema 없이 안전하게 해석 가능한 다중 source 비한정 참조만 resolve."""
    if schema is not None:
        return _resolve_unqualified_by_exposed_sources(
            col, sources, cte_names, collector, schema
        )

    physical_sources = [
        src for src in sources
        if _is_physical_table_source(src) and _source_exposes_column(
            col.name, src, cte_names, schema
        )
    ]
    if physical_sources:
        matches: List[_ResolvedColumn] = []
        candidates: List[str] = []
        for src in physical_sources:
            resolved = _resolve_via_source(
                col.name, src, cte_names, collector, schema
            )
            matches.extend(resolved)
            candidates.extend(item.table for item in resolved)
        if len(matches) == 1:
            return matches
        collector.add_ambiguous(col.name, None, candidates)
        return []

    explicit_matches: List[_ResolvedColumn] = []
    matched_sources = 0
    candidate_tables: List[str] = []
    for src in sources:
        if not _source_exposes_column(col.name, src, cte_names, schema):
            continue
        matched_sources += 1
        candidate_tables.extend(_physical_tables_in_source(src, cte_names))
        explicit_matches.extend(
            _resolve_via_source(col.name, src, cte_names, collector, schema)
        )

    if matched_sources == 1:
        return explicit_matches
    if matched_sources > 1:
        collector.add_ambiguous(col.name, None, candidate_tables)
        return []

    collector.add_unresolved(col.name, None, "multi_source_unresolved")
    return []


def _resolve_unqualified_by_exposed_sources(
    col: exp.Column,
    sources: List[object],
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    matches: List[_ResolvedColumn] = []
    candidate_tables: List[str] = []
    matched_sources = 0

    for src in sources:
        if not _source_exposes_column(col.name, src, cte_names, schema):
            continue
        matched_sources += 1
        resolved = _resolve_via_source(col.name, src, cte_names, collector, schema)
        matches.extend(resolved)
        if resolved:
            candidate_tables.extend(item.table for item in resolved)
        else:
            candidate_tables.extend(_physical_tables_in_source(src, cte_names))

    if matched_sources == 1:
        return _dedupe_resolved(matches)
    if matched_sources > 1:
        collector.add_ambiguous(col.name, None, candidate_tables)
        return []

    collector.add_unresolved(col.name, None, "multi_source_unresolved")
    return []


def _resolve_via_source(
    col_name: str,
    src: object,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    if isinstance(src, exp.Table):
        if _is_cte_ref(src, cte_names) or _is_dual(src):
            return []
        has_column = _schema_has_column(schema, src.name, col_name)
        if has_column is False:
            return []
        return [_ResolvedColumn(table=src.name.upper(), column=col_name.upper())]
    if isinstance(src, Scope):
        return _resolve_via_scope_projection(
            col_name, src, cte_names, collector, schema
        )
    return []


def _resolve_via_scope_projection(
    col_name: str,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    expr = scope.expression
    if isinstance(expr, exp.Select):
        return _resolve_select_projection(
            col_name, expr, scope, cte_names, collector, schema
        )
    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        return _resolve_setop_projection(col_name, expr, cte_names, collector, schema)
    return []


def _resolve_setop_projection(
    col_name: str,
    expr: exp.Expression,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    results: List[_ResolvedColumn] = []
    for select in _setop_leaf_selects(expr):
        leaf_scope = _find_scope_for_expression(expr, select)
        if leaf_scope is None:
            continue
        results.extend(
            _resolve_select_projection(
                col_name, select, leaf_scope, cte_names, collector, schema
            )
        )
    return _dedupe_resolved(results)


def _resolve_select_projection(
    col_name: str,
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    upper = col_name.upper()
    explicit_match = False
    explicit_results: List[_ResolvedColumn] = []

    for proj in select_expr.expressions:
        if _is_projection_star(proj):
            continue
        if _projection_name(proj) != upper:
            continue
        explicit_match = True
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        explicit_results.extend(
            _collect_columns_in_expression(inner, scope, cte_names, collector, schema)
        )

    if explicit_match:
        return _dedupe_resolved(explicit_results)

    star_results: List[_ResolvedColumn] = []
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            for src in _scope_sources(scope).values():
                star_results.extend(
                    _resolve_via_source(col_name, src, cte_names, collector, schema)
                )
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = _scope_sources(scope).get(proj.table)
            if src is not None:
                star_results.extend(
                    _resolve_via_source(col_name, src, cte_names, collector, schema)
                )
    return _dedupe_resolved(star_results)


def _collect_columns_in_expression(
    expr: exp.Expression,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    results: List[_ResolvedColumn] = []
    for col in expr.find_all(exp.Column):
        if col.is_star:
            continue
        if col.find_ancestor(exp.Select) is not scope.expression:
            continue
        results.extend(
            _resolve_column_in_scope_chain(col, scope, cte_names, collector, schema)
        )

    for select in expr.find_all(exp.Select):
        if select is scope.expression:
            continue
        nested_scope = _find_scope_for_expression(scope.expression, select)
        if nested_scope is None:
            continue
        results.extend(
            _collect_scope_columns_with_externals(
                nested_scope, cte_names, collector, schema
            )
        )
    return _dedupe_resolved(results)


def _collect_scope_columns_with_externals(
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    results: List[_ResolvedColumn] = []
    external_ids = {id(col) for col in scope.external_columns}

    for col in scope.columns:
        if isinstance(col.this, exp.Star) or col.is_star:
            continue
        target_scope = scope.parent if id(col) in external_ids else scope
        if target_scope is None:
            target_scope = scope
        results.extend(
            _resolve_column_in_scope_chain(
                col, target_scope, cte_names, collector, schema
            )
        )

    return _dedupe_resolved(results)


def _resolve_column_in_scope_chain(
    col: exp.Column,
    scope: Scope,
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> List[_ResolvedColumn]:
    if not col.table:
        return _resolve_column(col, scope, cte_names, collector, schema)

    current: Optional[Scope] = scope
    while current is not None:
        if col.table in _scope_sources(current):
            return _resolve_column(col, current, cte_names, collector, schema)
        current = current.parent

    collector.add_unresolved(col.name, col.table, "unknown_qualifier")
    return []


def _collect_projection_star_from_source(
    src: object,
    qualifier: Optional[str],
    cte_names: Set[str],
    collector: _Collector,
    schema: ReferenceSchema,
) -> None:
    if isinstance(src, exp.Table):
        if _is_cte_ref(src, cte_names) or _is_dual(src):
            return
        collector.add_projection_star(src.name, qualifier)
        return

    if not isinstance(src, Scope):
        return
    expr = src.expression
    if isinstance(expr, exp.Select):
        _collect_select_projection(expr, src, cte_names, collector, schema)
        return

    if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
        for select in _setop_leaf_selects(expr):
            leaf_scope = _find_scope_for_expression(expr, select)
            if leaf_scope is not None:
                _collect_select_projection(
                    select, leaf_scope, cte_names, collector, schema
                )


def _source_exposes_column(
    col_name: str,
    src: object,
    cte_names: Set[str],
    schema: ReferenceSchema,
) -> bool:
    if isinstance(src, exp.Table):
        if _is_cte_ref(src, cte_names) or _is_dual(src):
            return False
        has_column = _schema_has_column(schema, src.name, col_name)
        return True if has_column is None else has_column
    if isinstance(src, Scope):
        expr = src.expression
        if isinstance(expr, exp.Select):
            upper = col_name.upper()
            for proj in expr.expressions:
                if _projection_name(proj) == upper:
                    return True
            return _select_star_exposes_column(col_name, expr, src, cte_names, schema)
        if isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
            return any(
                _source_exposes_column(col_name, leaf_scope, cte_names, schema)
                for leaf_scope in _leaf_scopes(expr)
            )
    return False


def _select_star_exposes_column(
    col_name: str,
    select_expr: exp.Select,
    scope: Scope,
    cte_names: Set[str],
    schema: ReferenceSchema,
) -> bool:
    for proj in select_expr.expressions:
        if isinstance(proj, exp.Star):
            if any(
                _source_exposes_column(col_name, src, cte_names, schema)
                for src in _scope_sources(scope).values()
            ):
                return True
            continue
        if isinstance(proj, exp.Column) and proj.is_star:
            src = _scope_sources(scope).get(proj.table)
            if src is not None and _source_exposes_column(
                col_name, src, cte_names, schema
            ):
                return True
    return False


def _scope_sources(scope: Scope) -> Dict[str, object]:
    return dict(scope.sources)


def _is_physical_table_source(src: object) -> bool:
    return isinstance(src, exp.Table)


def _physical_tables_in_source(src: object, cte_names: Set[str]) -> List[str]:
    if isinstance(src, exp.Table):
        if _is_cte_ref(src, cte_names) or _is_dual(src):
            return []
        return [src.name.upper()]
    if isinstance(src, Scope):
        out: List[str] = []
        for table in src.expression.find_all(exp.Table):
            if _is_cte_ref(table, cte_names) or _is_dual(table):
                continue
            out.append(table.name.upper())
        return _dedupe_upper(out)
    return []


def _projection_name(proj: exp.Expression) -> Optional[str]:
    if isinstance(proj, exp.Alias):
        return proj.alias.upper()
    if isinstance(proj, exp.Column) and not proj.is_star:
        return proj.name.upper()
    return None


def _is_projection_star(proj: exp.Expression) -> bool:
    return isinstance(proj, exp.Star) or (
        isinstance(proj, exp.Column) and proj.is_star
    )


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


def _leaf_scopes(expr: exp.Expression) -> Iterable[Scope]:
    for select in _setop_leaf_selects(expr):
        scope = _find_scope_for_expression(expr, select)
        if scope is not None:
            yield scope


def _find_scope_for_expression(
    root: exp.Expression, target: exp.Expression
) -> Optional[Scope]:
    root_scope = build_scope(root)
    if root_scope is None:
        return None
    for scope in root_scope.traverse():
        if scope.expression is target:
            return scope
    return None


def _dedupe_upper(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        upper = item.upper()
        if upper in seen:
            continue
        seen.add(upper)
        out.append(upper)
    return out


def _dedupe_resolved(items: Iterable[_ResolvedColumn]) -> List[_ResolvedColumn]:
    seen: Set[Tuple[str, str]] = set()
    out: List[_ResolvedColumn] = []
    for item in items:
        key = (item.table.upper(), item.column.upper())
        if key in seen:
            continue
        seen.add(key)
        out.append(_ResolvedColumn(table=key[0], column=key[1]))
    return out

