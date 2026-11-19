"""
policy.py

OPA 로부터 받는 권한 응답을 파이썬 타입으로 표현한다.

현재 OPA 스키마:

    {
      "tables": {
        "<TABLE_NAME>": {
          "all_columns": ["COL1", "COL2", ...],
          "allowed_columns": ["COL1", "COL2", ...],
          "row_filter": "COL1 = 'value'"
        }
      }
    }

레거시 all_sources / allowed_sources 스키마도 하위 호환으로 파싱한다:

    {
      "all_sources": [
        {"scope_1": "<TABLE_NAME>", "scope_2": ["COL1", "COL2", ...]}
      ],
      "sql_query": "...",
      "allowed_sources": [
        {"scope_1": "<TABLE_NAME>", "scope_2": ["*"], "filter": "..."}
      ]
    }

초기 협의안인 top-level row_filters 배열도 하위 호환으로 파싱한다.

정규화 규칙: 테이블/컬럼명은 모두 대문자로 저장. schema-qualified source
이름은 "SCHEMA.TABLE" 형태를 유지한다.

호출 API 에서 위 OPA 결과가 "policy" 키 아래로 감싸져 들어오는 envelope 은
호환성 차원에서 자동으로 unwrap 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

import sqlglot
from sqlglot import exp

__all__ = [
    "RowFilter",
    "TablePolicy",
    "Policy",
    "PolicyParseError",
]


class PolicyParseError(Exception):
    """OPA 응답이 계약된 스키마를 위반."""


@dataclass(frozen=True)
class RowFilter:
    column: str = ""                 # 대문자 정규화된 컬럼명
    allowed_values: Sequence[str] = field(default_factory=tuple)
    expression_sql: str = ""         # 신규 OPA filter raw SQL predicate
    referenced_columns: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.expression_sql:
            return
        if not self.column:
            raise PolicyParseError("row_filter.column is empty")
        if not self.allowed_values:
            raise PolicyParseError(
                f"row_filter for column {self.column!r} has no allowed_values — "
                "empty list would deny all rows; OPA must omit the entry or "
                "the caller should treat the table as fully forbidden"
            )

    @property
    def is_raw_expression(self) -> bool:
        return bool(self.expression_sql)

    @property
    def filter_columns(self) -> Sequence[str]:
        if self.expression_sql and self.referenced_columns:
            return tuple(self.referenced_columns)
        if self.column:
            return (self.column,)
        return tuple(self.referenced_columns)


@dataclass
class TablePolicy:
    table: str                      # 대문자 정규화된 물리 테이블명
    all_columns: List[str]          # 물리 테이블의 전체 컬럼 (대문자, 순서 유지)
    allowed_columns: List[str]      # 사용자가 볼 수 있는 컬럼 (대문자, 순서 유지)
    row_filters: List[RowFilter] = field(default_factory=list)

    @property
    def row_filter(self) -> str:
        """현재 OPA 계약의 단일 row_filter 문자열 형태로 반환."""
        return _row_filters_to_sql(self.row_filters)

    @property
    def fully_denied(self) -> bool:
        return len(self.allowed_columns) == 0

    def is_allowed(self, column: str) -> bool:
        return column.upper() in self._allowed_set

    def has_column(self, column: str) -> bool:
        return column.upper() in self._all_set

    def is_banned(self, column: str) -> bool:
        upper = column.upper()
        return upper in self._all_set and upper not in self._allowed_set

    def __post_init__(self) -> None:
        self._all_set = {c.upper() for c in self.all_columns}
        self._allowed_set = {c.upper() for c in self.allowed_columns}
        missing = self._allowed_set - self._all_set
        if missing:
            raise PolicyParseError(
                f"tables[{self.table!r}].allowed_columns contains columns "
                f"not in all_columns: {sorted(missing)}"
            )
        for rf in self.row_filters:
            for column in rf.filter_columns:
                if column.upper() in self._all_set:
                    continue
                raise PolicyParseError(
                    f"row_filter {self.table}.{column} references a column "
                    "not in all_columns"
                )


@dataclass
class Policy:
    """한 번의 rewrite 에 필요한 모든 테이블의 정책."""
    tables: Dict[str, TablePolicy]

    def for_table(self, name: str) -> Optional[TablePolicy]:
        normalized = _normalize_source_name(name)
        exact = self.tables.get(normalized)
        if exact is not None:
            return exact

        table_part = normalized.rsplit(".", 1)[-1]
        if "." in normalized:
            unqualified = self.tables.get(table_part)
            if unqualified is not None:
                return unqualified
            return None

        suffix_matches = [
            table_policy
            for source, table_policy in self.tables.items()
            if source.rsplit(".", 1)[-1] == table_part
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return None

    # ── OPA 응답 파싱 ────────────────────────────────────────
    @classmethod
    def from_opa_response(cls, data: Mapping) -> "Policy":
        if not isinstance(data, Mapping):
            raise PolicyParseError(
                f"policy response must be a mapping, got {type(data).__name__}"
            )

        data = _unwrap_policy_envelope(data)

        if "tables" in data:
            return cls(tables=_parse_tables_policy(data))

        if "all_sources" in data or "allowed_sources" in data:
            return cls(tables=_parse_legacy_source_policy(data))

        raise PolicyParseError("response.tables is missing or not a mapping")


def _unwrap_policy_envelope(data: Mapping) -> Mapping:
    raw_policy = data.get("policy")
    if isinstance(raw_policy, Mapping) and (
        "all_sources" in raw_policy
        or "allowed_sources" in raw_policy
        or "tables" in raw_policy
    ):
        return raw_policy
    return data


def _parse_tables_policy(data: Mapping) -> Dict[str, TablePolicy]:
    raw_tables = data.get("tables")
    if not isinstance(raw_tables, Mapping):
        raise PolicyParseError("response.tables is missing or not a mapping")

    row_filters_by_table: Dict[str, List[RowFilter]] = {}
    for entry in data.get("row_filters", []) or []:
        rf, table = _parse_legacy_row_filter(entry)
        row_filters_by_table.setdefault(table, []).append(rf)

    tables: Dict[str, TablePolicy] = {}
    for raw_name, raw_body in raw_tables.items():
        name = _normalize_source_name(raw_name)
        if not name:
            raise PolicyParseError("tables contains an empty table name")
        if name in tables:
            raise PolicyParseError(f"duplicate table entry {name!r}")
        tables[name] = _parse_table_policy(
            name, raw_body, row_filters_by_table.get(name, [])
        )

    unknown = set(row_filters_by_table) - set(tables)
    if unknown:
        raise PolicyParseError(
            f"row_filters reference tables not in tables block: {sorted(unknown)}"
        )

    return tables


def _parse_legacy_source_policy(data: Mapping) -> Dict[str, TablePolicy]:
    raw_all_sources = data.get("all_sources")
    if not isinstance(raw_all_sources, (list, tuple)):
        raise PolicyParseError("all_sources is missing or not a list")

    all_columns_by_source: Dict[str, List[str]] = {}
    for index, entry in enumerate(raw_all_sources):
        if not isinstance(entry, Mapping):
            raise PolicyParseError(
                f"all_sources[{index}] must be a mapping"
            )
        source = _parse_scope_1(entry, f"all_sources[{index}].scope_1")
        if source in all_columns_by_source:
            raise PolicyParseError(f"duplicate all_sources entry {source!r}")
        all_columns_by_source[source] = _parse_column_list(
            entry.get("scope_2"),
            f"all_sources[{index}].scope_2",
        )

    allowed_by_source: Dict[str, List[str]] = {
        source: [] for source in all_columns_by_source
    }
    filters_by_source: Dict[str, List[RowFilter]] = {
        source: [] for source in all_columns_by_source
    }

    raw_allowed_sources = data.get("allowed_sources", []) or []
    if not isinstance(raw_allowed_sources, (list, tuple)):
        raise PolicyParseError("allowed_sources must be a list")

    seen_allowed = set()
    for index, entry in enumerate(raw_allowed_sources):
        if not isinstance(entry, Mapping):
            raise PolicyParseError(
                f"allowed_sources[{index}] must be a mapping"
            )
        source = _parse_scope_1(
            entry, f"allowed_sources[{index}].scope_1"
        )
        if source not in all_columns_by_source:
            raise PolicyParseError(
                f"allowed_sources references source not in all_sources: {source!r}"
            )
        if source in seen_allowed:
            raise PolicyParseError(f"duplicate allowed_sources entry {source!r}")
        seen_allowed.add(source)

        allowed_by_source[source] = _parse_allowed_scope_2(
            entry.get("scope_2"),
            all_columns_by_source[source],
            f"allowed_sources[{index}].scope_2",
        )

        raw_filter = entry.get("filter", "")
        if raw_filter is None:
            raw_filter = ""
        if not isinstance(raw_filter, str):
            raise PolicyParseError(
                f"allowed_sources[{index}].filter must be a string"
            )
        if raw_filter.strip():
            filters_by_source[source].extend(
                _parse_source_filter(raw_filter.strip())
            )

    tables: Dict[str, TablePolicy] = {}
    for source, all_columns in all_columns_by_source.items():
        tables[source] = TablePolicy(
            table=source,
            all_columns=all_columns,
            allowed_columns=allowed_by_source[source],
            row_filters=filters_by_source[source],
        )
    return tables


def _parse_table_policy(
    name: str, raw: Mapping, row_filters: List[RowFilter]
) -> TablePolicy:
    if not isinstance(raw, Mapping):
        raise PolicyParseError(f"tables[{name!r}] must be a mapping")

    all_columns = _parse_column_list(
        raw.get("all_columns"), f"tables[{name!r}].all_columns"
    )
    allowed = _parse_allowed_scope_2(
        raw.get("allowed_columns"),
        all_columns,
        f"tables[{name!r}].allowed_columns",
    )
    filters = list(row_filters)

    raw_filter = raw.get("row_filter", "")
    if raw_filter is None:
        raw_filter = ""
    if not isinstance(raw_filter, str):
        raise PolicyParseError(f"tables[{name!r}].row_filter must be a string")
    if raw_filter.strip():
        filters.extend(_parse_source_filter(raw_filter.strip()))

    return TablePolicy(
        table=name,
        all_columns=all_columns,
        allowed_columns=allowed,
        row_filters=filters,
    )


def _parse_column_list(raw, label: str) -> List[str]:
    if raw is None:
        raise PolicyParseError(f"{label} is missing")
    if not isinstance(raw, (list, tuple)):
        raise PolicyParseError(f"{label} must be a list")
    out: List[str] = []
    seen = set()
    for item in raw:
        if item is None:
            raise PolicyParseError(f"{label} contains null")
        col = str(item).strip().upper()
        if not col:
            raise PolicyParseError(f"{label} contains empty column name")
        if col in seen:
            raise PolicyParseError(f"{label} has duplicate {col!r}")
        seen.add(col)
        out.append(col)
    return out


def _parse_allowed_scope_2(raw, all_columns: List[str], label: str) -> List[str]:
    columns = _parse_column_list(raw, label)
    if columns == ["*"]:
        return list(all_columns)
    if "*" in columns:
        raise PolicyParseError(f"{label} can be ['*'] or explicit columns, not both")
    return columns


def _parse_scope_1(entry: Mapping, label: str) -> str:
    source = _normalize_source_name(entry.get("scope_1", ""))
    if not source:
        raise PolicyParseError(f"{label} is missing or empty")
    return source


def _parse_legacy_row_filter(entry) -> tuple:
    if not isinstance(entry, Mapping):
        raise PolicyParseError(
            f"row_filters entry must be a mapping, got {type(entry).__name__}"
        )
    table = _normalize_source_name(entry.get("table", ""))
    if not table:
        raise PolicyParseError("row_filters entry missing 'table'")
    raw_column = entry.get("column", "")
    column = "" if raw_column is None else str(raw_column).upper()
    values = entry.get("allowed_values")
    if values is None or not isinstance(values, (list, tuple)):
        raise PolicyParseError(
            f"row_filters[{table}.{column}].allowed_values must be a list"
        )
    rf = RowFilter(column=column, allowed_values=tuple(str(v) for v in values))
    return rf, table


def _parse_source_filter(raw_filter: str) -> List[RowFilter]:
    try:
        parsed_items = [
            expr for expr in sqlglot.parse(raw_filter, read="oracle")
            if expr is not None
        ]
    except Exception:
        return [RowFilter(
            expression_sql=raw_filter,
            referenced_columns=(),
        )]
    if len(parsed_items) != 1:
        return [RowFilter(
            expression_sql=raw_filter,
            referenced_columns=(),
        )]
    parsed = parsed_items[0]

    parts = list(_split_and(parsed))
    structured: List[RowFilter] = []
    for part in parts:
        rf = _simple_row_filter(part)
        if rf is None:
            columns = tuple(_columns_in_expression(parsed))
            first = columns[0] if columns else ""
            return [RowFilter(
                column=first,
                expression_sql=raw_filter,
                referenced_columns=columns,
            )]
        structured.append(rf)
    return structured


def _split_and(expr: exp.Expression):
    if isinstance(expr, exp.And):
        yield from _split_and(expr.this)
        yield from _split_and(expr.expression)
        return
    yield expr


def _simple_row_filter(expr: exp.Expression) -> Optional[RowFilter]:
    if isinstance(expr, exp.EQ) and isinstance(expr.this, exp.Column):
        value = _literal_to_string(expr.expression)
        if value is not None:
            return RowFilter(
                column=expr.this.name.upper(),
                allowed_values=(value,),
            )
    if isinstance(expr, exp.In) and isinstance(expr.this, exp.Column):
        values = [_literal_to_string(item) for item in expr.expressions]
        if values and all(value is not None for value in values):
            return RowFilter(
                column=expr.this.name.upper(),
                allowed_values=tuple(value for value in values if value is not None),
            )
    return None


def _literal_to_string(expr: exp.Expression) -> Optional[str]:
    if not isinstance(expr, exp.Literal):
        return None
    return str(expr.this)


def _row_filters_to_sql(row_filters: Sequence[RowFilter]) -> str:
    parts: List[str] = []
    for rf in row_filters:
        if rf.is_raw_expression:
            parts.append(rf.expression_sql)
            continue
        values = list(rf.allowed_values)
        if len(values) == 1:
            parts.append(f"{rf.column} = {_sql_string(values[0])}")
        else:
            parts.append(
                f"{rf.column} IN ({', '.join(_sql_string(value) for value in values)})"
            )
    return " AND ".join(parts)


def _sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _columns_in_expression(expr: exp.Expression) -> List[str]:
    out: List[str] = []
    seen = set()
    for col in expr.find_all(exp.Column):
        name = col.name.upper()
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _normalize_source_name(value) -> str:
    if value is None:
        return ""
    return ".".join(
        part.strip().upper()
        for part in str(value).split(".")
        if part.strip()
    )

