"""
국가핵심기술(DRAM / HBM) 제품 적재 현황 조사 스크립트

[동작 순서]
1. input txt 파일(table_name, column_name)을 읽는다.
2. 테이블별로 컬럼명에 'prod_id'가 들어간 컬럼을 찾는다. (DB 조회 없이 input만 사용)
3. 여러 테이블을 동시에(병렬로) StarRocks에 조회해서 아래 값을 구한다.
   - 총 Row 수
   - DRAM / HBM 국가핵심기술 Row Count, 제품 수
4. 결과를 엑셀 파일로 저장한다.

[실행 방법]
    python check_secu_prod_tables.py

[필요 패키지]
    pip install pandas openpyxl python-dotenv
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import dotenv
import pandas as pd
from lakes.client import LakeHouse

dotenv.load_dotenv()


# ============================================================
# 설정값 (필요하면 여기만 바꾸면 됩니다)
# ============================================================
INPUT_FILE = "mes_table.txt"
OUTPUT_FILE = "국가핵심기술_적재현황.xlsx"
CATALOG = "lake_catalog"

# 동시에 조회할 테이블 개수. 너무 크게 잡으면 StarRocks에 부하가 갈 수 있으니 4~10 정도 권장
MAX_WORKERS = 8

# 국가핵심기술(DRAM) 제품 ID 목록을 구하는 서브쿼리
DRAM_PROD_SUBQUERY = """
    SELECT prod_id
    FROM EHUB.ehub_hub_gif_prod_m
    WHERE prod_family_val = 'DRAM'
      AND secu_prod_yn = 'Y'
"""

# 국가핵심기술(HBM) 제품 ID 목록을 구하는 서브쿼리
HBM_PROD_SUBQUERY = """
    SELECT prod_id
    FROM EHUB.ehub_hub_gif_prod_m
    WHERE prod_family_val = 'DRAM'
      AND secu_prod_yn = 'Y'
      AND UPPER(prod_mode_val) LIKE '%HBM%'
"""


# ============================================================
# LakeHouse 연결 (스레드마다 1개씩 만들어서 사용)
# ============================================================
# 여러 스레드가 연결 1개를 같이 쓰면 충돌할 수 있어서, 스레드별로 따로 만들어 둡니다.
thread_storage = threading.local()


def get_lakehouse():
    if not hasattr(thread_storage, "lakehouse"):
        thread_storage.lakehouse = LakeHouse()
    return thread_storage.lakehouse


def run_count_query(query):
    """COUNT 결과 1개(숫자)를 돌려주는 쿼리를 실행하고, 그 숫자를 반환한다."""
    lakehouse = get_lakehouse()
    result = lakehouse.execute_pd(query=query, catalog=CATALOG)
    value = result.iloc[0, 0]
    if pd.isna(value):
        return 0
    return int(value)


# ============================================================
# Step 1, 2. input 파일 읽기 + 제품 ID 컬럼 찾기
# ============================================================
def load_tables_and_prod_id_columns(input_file):
    """
    input 파일을 읽어서
    {테이블명: [제품 ID 컬럼1, 제품 ID 컬럼2, ...]} 형태의 딕셔너리로 만든다.
    테이블 순서는 input 파일에 나온 순서를 그대로 유지한다.
    """
    input_df = pd.read_csv(input_file, sep="\t", dtype=str)
    input_df["table_name"] = input_df["table_name"].str.strip()
    input_df["column_name"] = input_df["column_name"].str.strip()

    table_to_prod_columns = {}

    for _, row in input_df.iterrows():
        table_name = row["table_name"]
        column_name = row["column_name"]

        if pd.isna(table_name) or table_name == "":
            continue

        # 제품 ID 컬럼이 없는 테이블도 결과에 나와야 하므로 먼저 빈 리스트로 등록
        if table_name not in table_to_prod_columns:
            table_to_prod_columns[table_name] = []

        if pd.isna(column_name):
            continue

        # '%prod_id%' 조건: 대소문자 구분 없이 'prod_id'가 포함된 컬럼
        is_prod_id_column = "prod_id" in column_name.lower()
        already_added = column_name in table_to_prod_columns[table_name]

        if is_prod_id_column and not already_added:
            table_to_prod_columns[table_name].append(column_name)

    return table_to_prod_columns


# ============================================================
# Step 3, 4, 5. 국가핵심기술 Row Count / 제품 수 쿼리 만들기
# ============================================================
def build_row_count_query(table_name, prod_id_columns, prod_subquery):
    """
    제품 ID 컬럼 중 하나라도 국가핵심기술 제품 ID에 해당하면 1개 Row로 센다.
    (컬럼이 여러 개면 OR 조건으로 연결)
    """
    conditions = []
    for column_name in prod_id_columns:
        conditions.append(f"`{column_name}` IN ({prod_subquery})")

    where_condition = "\n       OR ".join(conditions)

    query = f"""
    SELECT COUNT(*)
    FROM {table_name}
    WHERE {where_condition}
    """
    return query


def build_prod_count_query(table_name, prod_id_columns, prod_subquery):
    """
    테이블에 들어있는 국가핵심기술 제품 ID의 distinct 개수.
    컬럼이 여러 개면, 어느 컬럼에든 한 번이라도 나온 제품을 1개로 센다. (중복 제거)

    큰 테이블을 펼치지 않도록, 작은 제품 목록 쪽을 기준으로
    "이 제품이 테이블에 있는가?"를 확인하는 방식으로 센다.
    """
    conditions = []
    for column_name in prod_id_columns:
        conditions.append(
            f"secu_prod.prod_id IN (SELECT `{column_name}` FROM {table_name})"
        )

    where_condition = "\n       OR ".join(conditions)

    query = f"""
    SELECT COUNT(DISTINCT secu_prod.prod_id)
    FROM ({prod_subquery}) AS secu_prod
    WHERE {where_condition}
    """
    return query


def build_total_row_query(table_name):
    return f"SELECT COUNT(*) FROM {table_name}"


# ============================================================
# 테이블 1개 조사하기 (이 함수가 병렬로 여러 번 동시에 실행됨)
# ============================================================
def calculate_ratio(part_count, total_count):
    if total_count == 0:
        return 0.0
    return round(part_count / total_count * 100, 4)


def investigate_table(table_name, prod_id_columns):
    # 기본값 (조회 실패하거나 제품 ID 컬럼이 없을 때 이 값으로 남음)
    table_result = {
        "테이블명": table_name,
        "제품 ID 컬럼명": ", ".join(prod_id_columns),
        "국가핵심기술(DRAM) 보유 여부": "N",
        "국가핵심기술(DRAM) Row Count": 0,
        "국가핵심기술(DRAM) 제품 수": 0,
        "국가핵심기술(HBM) 보유 여부": "N",
        "국가핵심기술(HBM) Row Count": 0,
        "국가핵심기술(HBM) 제품 수": 0,
        "총 Row 수": None,
        "DRAM Row 비율": 0.0,
        "HBM Row 비율": 0.0,
    }
    error_message = None

    try:
        # Step 6. 총 Row 수
        total_row_count = run_count_query(build_total_row_query(table_name))
        table_result["총 Row 수"] = total_row_count

        # 제품 ID 컬럼이 없거나, 테이블이 비어 있으면 더 조회할 필요 없음
        if len(prod_id_columns) == 0:
            error_message = "제품 ID 컬럼 없음 (DRAM/HBM 조회 생략)"
            return table_result, error_message
        if total_row_count == 0:
            return table_result, error_message

        # Step 3. DRAM Row Count
        dram_row_count = run_count_query(
            build_row_count_query(table_name, prod_id_columns, DRAM_PROD_SUBQUERY)
        )

        # DRAM Row가 0이면, HBM(DRAM의 일부)도 0이므로 나머지 조회 생략
        if dram_row_count == 0:
            return table_result, error_message

        # Step 4. DRAM 제품 수
        dram_prod_count = run_count_query(
            build_prod_count_query(table_name, prod_id_columns, DRAM_PROD_SUBQUERY)
        )

        # Step 5. HBM Row Count / 제품 수
        hbm_row_count = run_count_query(
            build_row_count_query(table_name, prod_id_columns, HBM_PROD_SUBQUERY)
        )
        hbm_prod_count = 0
        if hbm_row_count > 0:
            hbm_prod_count = run_count_query(
                build_prod_count_query(table_name, prod_id_columns, HBM_PROD_SUBQUERY)
            )

        # Step 7. 보유 여부(Y/N) / 비율
        table_result["국가핵심기술(DRAM) Row Count"] = dram_row_count
        table_result["국가핵심기술(DRAM) 제품 수"] = dram_prod_count
        table_result["국가핵심기술(DRAM) 보유 여부"] = "Y" if dram_row_count >= 1 else "N"

        table_result["국가핵심기술(HBM) Row Count"] = hbm_row_count
        table_result["국가핵심기술(HBM) 제품 수"] = hbm_prod_count
        table_result["국가핵심기술(HBM) 보유 여부"] = "Y" if hbm_row_count >= 1 else "N"

        table_result["DRAM Row 비율"] = calculate_ratio(dram_row_count, total_row_count)
        table_result["HBM Row 비율"] = calculate_ratio(hbm_row_count, total_row_count)

    except Exception as error:
        error_message = f"조회 실패: {error}"

    return table_result, error_message


# ============================================================
# 메인 실행
# ============================================================
def main():
    start_time = time.time()

    table_to_prod_columns = load_tables_and_prod_id_columns(INPUT_FILE)
    table_names = list(table_to_prod_columns.keys())
    total_table_count = len(table_names)
    print(f"조사 대상 테이블: {total_table_count}개 (동시 조회 {MAX_WORKERS}개)")

    results_by_table = {}
    errors = []
    finished_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 모든 테이블 조사를 작업으로 등록
        future_to_table = {}
        for table_name in table_names:
            prod_id_columns = table_to_prod_columns[table_name]
            future = executor.submit(investigate_table, table_name, prod_id_columns)
            future_to_table[future] = table_name

        # 끝나는 순서대로 결과 받기
        for future in as_completed(future_to_table):
            table_name = future_to_table[future]
            table_result, error_message = future.result()

            results_by_table[table_name] = table_result
            if error_message:
                errors.append({"테이블명": table_name, "내용": error_message})

            finished_count += 1
            print(
                f"[{finished_count}/{total_table_count}] {table_name} "
                f"- DRAM {table_result['국가핵심기술(DRAM) Row Count']}건, "
                f"HBM {table_result['국가핵심기술(HBM) Row Count']}건"
                + (f"  ※ {error_message}" if error_message else "")
            )

    # input 파일에 있던 테이블 순서대로 정렬해서 저장
    result_rows = [results_by_table[table_name] for table_name in table_names]
    result_df = pd.DataFrame(result_rows)
    error_df = pd.DataFrame(errors, columns=["테이블명", "내용"])

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        result_df.to_excel(writer, sheet_name="적재현황", index=False)
        error_df.to_excel(writer, sheet_name="확인필요", index=False)

    elapsed_minutes = (time.time() - start_time) / 60
    dram_table_count = (result_df["국가핵심기술(DRAM) 보유 여부"] == "Y").sum()
    hbm_table_count = (result_df["국가핵심기술(HBM) 보유 여부"] == "Y").sum()

    print("=" * 60)
    print(f"완료: {OUTPUT_FILE} (소요 {elapsed_minutes:.1f}분)")
    print(f"DRAM 보유 테이블: {dram_table_count}개 / HBM 보유 테이블: {hbm_table_count}개")
    print(f"확인 필요(제품 ID 컬럼 없음, 조회 실패): {len(error_df)}개")


if __name__ == "__main__":
    main()
