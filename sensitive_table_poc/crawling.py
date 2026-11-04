"""Data Lake 적재 테이블의 컬럼별 샘플 값(sample value) 크롤링용 SQL 생성.

테이블 1개에 대해, 지정한 컬럼들의 고유값과 빈도를 한 번의 쿼리로 가져오는 StarRocks SQL을 만든다.
결과는 "column_name | val | cnt" 형태의 표 하나로 나온다.

주의
- ID·일시·금액처럼 고유값이 매우 많은 컬럼은 넣지 않는다.
  (결과는 top_n개로 잘리지만, 그 전에 DB가 모든 고유값을 집계해야 해서 부하가 크다)
- 개인정보 컬럼(성명, 연락처 등)은 실제 값이 그대로 추출되므로 넣지 않는다.
"""


def build_value_sql(table: str, columns: list[str], top_n: int = 1000, scan_limit: int = 1000000) -> str:
    """테이블의 여러 컬럼에 대해 컬럼별 고유값·빈도(상위 top_n개)를 한 번에 가져오는 SQL을 만든다.

    table      : "스키마.테이블명" (예: scm.scm_ddp_smpl_ship_req)
    columns    : 값을 가져올 컬럼명 목록
    top_n      : 컬럼당 가져올 최대 고유값 개수 (빈도 높은 순)
    scan_limit : 컬럼당 읽을 최대 행 수 (큰 테이블 부하 제한)
    """
    parts = []
    for col in columns:
        parts.append(
            f"    SELECT '{col}' AS column_name, CAST({col} AS VARCHAR(65533)) AS val, COUNT(*) AS cnt\n"
            f"    FROM (SELECT {col} FROM {table} LIMIT {scan_limit}) t\n"
            f"    WHERE {col} IS NOT NULL AND CAST({col} AS VARCHAR(65533)) <> ''\n"
            f"    GROUP BY {col}"
        )
    union_sql = "\n    UNION ALL\n".join(parts)
    return (
        "SELECT column_name, SUBSTR(val, 1, 50) AS val, cnt\n"
        "FROM (\n"
        "  SELECT column_name, val, cnt,\n"
        "         ROW_NUMBER() OVER (PARTITION BY column_name ORDER BY cnt DESC) AS rn\n"
        "  FROM (\n"
        f"{union_sql}\n"
        "  ) u\n"
        ") r\n"
        f"WHERE rn <= {top_n}\n"
        "ORDER BY column_name, cnt DESC;"
    )


if __name__ == "__main__":
    # 사용 예: 생성된 SQL을 화면에 출력
    sql = build_value_sql("scm.scm_ddp_smpl_ship_req", ["PROD_TYPE", "SHIP_TYPE"])
    print(sql)
