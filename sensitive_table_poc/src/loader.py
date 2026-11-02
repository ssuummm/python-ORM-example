"""입력 txt 파일을 읽는 모듈.

입력 파일은 모두 Excel에서 복사해 붙여넣은 txt(탭으로 구분)라고 가정한다.

- 키워드 파일 열: keyword, source, source_detail
- 크롤링 결과 파일 열: table_name, table_desc, sys_cd, schema, column_name, column_desc,
                      equivalent_columns, std_term, std_term_desc, data_type
"""
import pandas as pd

from .models import ColumnMeta, Keyword, TableMeta

# 사내 열 이름을 코드에서 쓰는 이름으로 바꾼다.
COLUMN_RENAME = {
    "sys_cd": "system_code",
    "schema": "schema_name",
}


def read_txt(path: str) -> pd.DataFrame:
    """탭으로 구분된 txt 파일을 DataFrame으로 읽는다."""
    # keep_default_na=False: 빈 칸을 NaN이 아닌 빈 문자열("")로 읽는다.
    try:
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        # 메모장에서 ANSI로 저장한 한글 파일은 cp949 인코딩이다.
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, encoding="cp949")

    df.columns = [col.strip().lower() for col in df.columns]
    df = df.rename(columns=COLUMN_RENAME)

    # 모든 칸의 앞뒤 공백 제거
    for col in df.columns:
        df[col] = df[col].str.strip()
    return df


def load_keywords(path: str, source: str) -> list[Keyword]:
    """키워드 파일을 읽는다. source는 "global"(전사) 또는 "system"(시스템별)."""
    df = read_txt(path)
    df = df[df["keyword"] != ""]                  # 키워드가 빈 행 제거
    df = df.drop_duplicates(subset=["keyword"])   # 같은 키워드 중복 제거

    prefix = "G" if source == "global" else "S"
    keywords = []
    for number, row in enumerate(df.to_dict("records"), start=1):
        keywords.append(Keyword(
            keyword_id=f"{prefix}-{number:03d}",  # 예: G-001, S-001
            keyword=row["keyword"],
            description=f"출처: {row['source']} / {row['source_detail']}",
            source=source,
        ))
    return keywords


def load_tables(path: str) -> list[TableMeta]:
    """크롤링 결과 파일을 읽는다. 파일은 컬럼 1개당 1행이므로 테이블 단위로 묶는다."""
    df = read_txt(path)
    tables = {}  # key: "스키마.테이블명", value: TableMeta

    for row in df.to_dict("records"):
        if row["table_name"] == "" or row["column_name"] == "":
            continue  # 빈 행 건너뜀

        table_key = f"{row['schema_name']}.{row['table_name']}"
        if table_key not in tables:
            tables[table_key] = TableMeta(
                system_code=row["system_code"],
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                table_desc=row["table_desc"],
            )

        tables[table_key].columns.append(ColumnMeta(
            column_name=row["column_name"],
            column_desc=row["column_desc"],
            std_term=row["std_term"],
            std_term_desc=row["std_term_desc"],
            data_type=row["data_type"],
        ))
    return list(tables.values())
