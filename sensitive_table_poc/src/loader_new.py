"""키워드 사전과 테이블 메타데이터 로더.

모든 파일은 pandas DataFrame으로 읽는다.
- .txt / .tsv : Excel에서 복사해 붙여넣은 탭 구분 텍스트
- .csv        : 쉼표 구분
- .xlsx       : 첫 번째 시트 (openpyxl 필요)
- .json       : 테이블 메타데이터 전용 (sample_data/tables.json 형식)

[키워드 파일]
  필수: keyword
  선택: keyword_id (없으면 자동 생성: 전사 G-001…, 시스템 S-001…)
        aliases     (| 구분. 없으면 keyword의 "수율 / Yield"처럼 / 로 나뉜 표기를 별칭으로 사용)
        source, source_detail (키워드 출처 → description에 기록되어 LLM에 함께 전달)
        category, level(HIGH/MEDIUM/LOW, 기본 HIGH), description
        system_code (시스템 키워드 전용. 없으면 모든 테이블에 적용)

[테이블 메타데이터 파일] 컬럼 1행
  system_code, schema_name, table_name, table_desc, column_name, column_desc,
  std_term, std_term_desc, data_type, sample_values(| 구분)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .models import ColumnMeta, Keyword, TableMeta

log = logging.getLogger(__name__)
_ENCODINGS = ("utf-8-sig", "cp949")   # Excel 붙여넣기 txt는 한글 Windows에서 cp949로 저장되는 경우가 있음


def read_table_file(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    elif suffix in (".txt", ".tsv", ".csv"):
        sep = "," if suffix == ".csv" else "\t"
        last_err = None
        for enc in _ENCODINGS:
            try:
                df = pd.read_csv(path, sep=sep, dtype=str, encoding=enc, keep_default_na=False)
                break
            except UnicodeDecodeError as e:
                last_err = e
        else:
            raise ValueError(f"{path}: 인코딩을 읽지 못함 (UTF-8/CP949) — {last_err}")
    else:
        raise ValueError(f"지원하지 않는 형식: {suffix} (txt/tsv/csv/xlsx)")

    # Excel 복사본 정리: 헤더 공백·대소문자, 빈 열(Unnamed), 셀 공백, 빈 행
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("unnamed")]]
    df = df.fillna("").astype(str).apply(lambda s: s.str.strip())
    df = df[(df != "").any(axis=1)].reset_index(drop=True)
    return df


def _split(v: str, sep: str = "|") -> list[str]:
    return [x.strip() for x in (v or "").split(sep) if x.strip()]


def _get(row: pd.Series, col: str) -> str:
    return row[col] if col in row.index else ""


def load_keywords(path: str | Path, source: str) -> list[Keyword]:
    df = read_table_file(path)
    if "keyword" not in df.columns:
        raise ValueError(f"{path}: 'keyword' 컬럼이 없음 (현재 컬럼: {list(df.columns)})")
    df = df[df["keyword"] != ""].drop_duplicates(subset=["keyword"]).reset_index(drop=True)

    prefix = "G" if source == "global" else "S"
    out: list[Keyword] = []
    for i, row in df.iterrows():
        raw_kw = row["keyword"]
        # "수율 / Yield" → keyword=수율, aliases=[Yield]
        parts = _split(raw_kw, "/") or [raw_kw]
        aliases = parts[1:] + _split(_get(row, "aliases"))

        desc = _get(row, "description")
        origin = " / ".join(x for x in (_get(row, "source"), _get(row, "source_detail")) if x)
        if origin:
            desc = f"{desc} (출처: {origin})" if desc else f"출처: {origin}"

        level = (_get(row, "level") or "HIGH").upper()
        if level not in ("HIGH", "MEDIUM", "LOW"):
            log.warning("%s: '%s'의 level '%s'를 HIGH로 처리", path, raw_kw, level)
            level = "HIGH"

        out.append(Keyword(
            keyword_id=_get(row, "keyword_id") or f"{prefix}-{i + 1:03d}",
            keyword=parts[0],
            aliases=list(dict.fromkeys(aliases)),
            category=_get(row, "category"),
            level=level,
            description=desc,
            source=source,
            system_code=_get(row, "system_code") or None,
        ))

    ids = [k.keyword_id for k in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"{path}: 중복 keyword_id {sorted(dup)}")
    if source == "system" and "system_code" not in df.columns:
        log.warning("%s: system_code 컬럼이 없어 시스템 키워드 %d개를 모든 테이블에 적용합니다.", path, len(out))
    log.info("%s 키워드 %d개 로드 (%s)", source, len(out), path)
    return out


def load_tables(path: str | Path) -> list[TableMeta]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return [TableMeta.model_validate(t) for t in data]

    df = read_table_file(path)
    required = {"system_code", "table_name", "column_name"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: 필수 컬럼 누락 {sorted(missing)} (현재 컬럼: {list(df.columns)})")

    tables: dict[tuple, TableMeta] = {}
    for _, row in df.iterrows():
        key = (row["system_code"], _get(row, "schema_name"), row["table_name"])
        t = tables.get(key)
        if t is None:
            t = tables[key] = TableMeta(system_code=key[0], schema_name=key[1], table_name=key[2],
                                        table_desc=_get(row, "table_desc"))
        if row["column_name"]:
            t.columns.append(ColumnMeta(
                column_name=row["column_name"],
                column_desc=_get(row, "column_desc"),
                std_term=_get(row, "std_term"),
                std_term_desc=_get(row, "std_term_desc"),
                data_type=_get(row, "data_type"),
                sample_values=_split(_get(row, "sample_values")),
            ))
    return list(tables.values())
