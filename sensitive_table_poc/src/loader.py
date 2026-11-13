"""입력 txt 파일을 읽는 모듈.

입력 파일은 모두 Excel에서 복사해 붙여넣은 txt(탭으로 구분)라고 가정한다.

- 키워드 파일(keywords.txt) 열: sys_cd, keyword, description, equivalent_terms, high_risk_tag,
                              source, source_detail, history
- 크롤링 결과 파일 열: table_name, table_desc, sys_cd, schema, column_name, column_desc,
                      equivalent_columns, std_term, std_term_desc, data_type
"""
import pandas as pd
from .models import ColumnMeta, Keyword, TableMeta

def read_txt(path: str) -> pd.DataFrame:
    """탭으로 구분된 txt 파일을 DataFrame으로 읽는다."""
    # keep_default_na=False: 빈 칸을 NaN이 아닌 빈 문자열("")로 읽는다.
    try:
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, encoding="cp949")

    df.columns = [col.strip().lower() for col in df.columns]

    # 모든 칸의 앞뒤 공백 제거
    for col in df.columns:
        df[col] = df[col].str.strip()
    return df


def load_keywords(path: str) -> list[Keyword]:
    """keywords.txt를 읽는다.

    열: sys_cd, keyword, description, equivalent_terms, high_risk_tag, source, source_detail, history
    - sys_cd가 "*"이면 모든 시스템, 아니면 해당 시스템 테이블에만 적용
    - equivalent_terms는 "|"로 구분 (예: Yield|YLD)
    - source, source_detail은 출처 기록용이라 읽지 않는다.
    - history가 "삭제"인 키워드는 제외한다.
    """
    df = read_txt(path)
    df = df[df["keyword"] != ""]                             # 키워드가 빈 행 제거
    df = df[df["history"] != "삭제"]                         # history가 '삭제'인 키워드 제외
    df = df.drop_duplicates(subset=["sys_cd", "keyword"])    # 같은 시스템의 같은 키워드 중복 제거

    keywords = []
    for number, row in enumerate(df.to_dict("records"), start=1):
        sys_cd = row["sys_cd"] or "*"                        # 빈 칸은 전체 적용(*)으로 간주
        equivalent_terms = [t.strip() for t in row["equivalent_terms"].split("|") if t.strip()]
        keywords.append(Keyword(
            keyword_id=f"K-{number:03d}",                    # 예: K-001
            keyword=row["keyword"],
            description=row["description"],
            equivalent_terms=equivalent_terms,
            high_risk_tag=row["high_risk_tag"],
            sys_cd=sys_cd,
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
                sys_cd=row["sys_cd"],
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                table_desc=row["table_desc"],
            )

        tables[table_key].columns.append(ColumnMeta(
            column_name=row["column_name"],
            column_desc=row["column_desc"],
            equivalent_columns=row["equivalent_columns"],
            std_term=row["std_term"],
            std_term_desc=row["std_term_desc"],
            data_type=row["data_type"],
            sample_values=[v.strip() for v in row["sample_values"].split("|") if v.strip()],
            sample_source=row["sample_source"],
        ))
    return list(tables.values())
