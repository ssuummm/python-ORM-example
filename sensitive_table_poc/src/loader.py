"""키워드 사전과 테이블 메타데이터 로더.

키워드 CSV 컬럼:
  keyword_id, keyword, aliases(| 구분), category, level(HIGH/MEDIUM/LOW), description, [system_code]

테이블 메타데이터:
  (a) JSON  : TableMeta 리스트 (sample_data/tables.json 참고)
  (b) CSV   : 컬럼 1행 단위 flat 형식 (메타데이터 관리 시스템 export 형태)
      system_code, schema_name, table_name, table_desc, column_name, column_desc,
      std_term, std_term_desc, data_type, sample_values(| 구분)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import ColumnMeta, Keyword, TableMeta


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def load_keywords(path: str | Path, source: str) -> list[Keyword]:
    out: list[Keyword] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("keyword_id") and row.get("keyword")):
                continue
            out.append(Keyword(
                keyword_id=row["keyword_id"].strip(),
                keyword=row["keyword"].strip(),
                aliases=_split(row.get("aliases")),
                category=(row.get("category") or "").strip(),
                level=(row.get("level") or "HIGH").strip().upper(),
                description=(row.get("description") or "").strip(),
                source=source,
                system_code=(row.get("system_code") or "").strip() or None,
            ))
    ids = [k.keyword_id for k in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"중복 keyword_id: {sorted(dup)}")
    return out


def load_tables(path: str | Path) -> list[TableMeta]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return [TableMeta.model_validate(t) for t in data]
    if path.suffix.lower() == ".csv":
        return _load_flat_csv(path)
    raise ValueError(f"지원하지 않는 형식: {path.suffix} (json/csv)")


def _load_flat_csv(path: Path) -> list[TableMeta]:
    tables: dict[tuple, TableMeta] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["system_code"].strip(), (row.get("schema_name") or "").strip(), row["table_name"].strip())
            t = tables.get(key)
            if t is None:
                t = tables[key] = TableMeta(
                    system_code=key[0], schema_name=key[1], table_name=key[2],
                    table_desc=(row.get("table_desc") or "").strip(),
                )
            if (row.get("column_name") or "").strip():
                t.columns.append(ColumnMeta(
                    column_name=row["column_name"].strip(),
                    column_desc=(row.get("column_desc") or "").strip(),
                    std_term=(row.get("std_term") or "").strip(),
                    std_term_desc=(row.get("std_term_desc") or "").strip(),
                    data_type=(row.get("data_type") or "").strip(),
                    sample_values=_split(row.get("sample_values")),
                ))
    return list(tables.values())
