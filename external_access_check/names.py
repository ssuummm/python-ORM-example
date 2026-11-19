"""Name normalization helpers shared by SQL AST walkers."""

from __future__ import annotations

from sqlglot import exp


def normalize_source_name(value) -> str:
    if value is None:
        return ""
    return ".".join(
        part.strip().upper()
        for part in str(value).split(".")
        if part.strip()
    )


def table_source_name(table: exp.Table) -> str:
    """Return the policy source name for a sqlglot Table node.

    sqlglot splits HIPASS2.VIEW_AI_SEARCH into db=HIPASS2 and
    name=VIEW_AI_SEARCH. OPA policy keys use the fully qualified source string.
    """

    parts = []
    for key in ("catalog", "db"):
        part = table.args.get(key)
        if part is not None:
            name = getattr(part, "name", None) or str(part)
            if name:
                parts.append(name)
    if table.name:
        parts.append(table.name)
    return normalize_source_name(".".join(parts))
