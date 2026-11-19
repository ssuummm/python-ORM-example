"""
verify_union_fix.py — UNION 컬럼 처리 수정 검증

실행:  python verify_union_fix.py        (불일치만 출력)
       python verify_union_fix.py -v     (모든 케이스의 변환 결과 출력)

각 케이스는 OPA 요청과 같은 모양(policy + sql_query)이다. 테이블 권한만
바꿔 가며 rewrite 결과의 "메인 쿼리" 부분이 기대값과 같은지 본다.
WITH 안쪽(테이블별 권한 적용)은 수정과 무관하므로 비교하지 않는다.

전제: T1, T2, T3 모두 컬럼은 ID, ITEM. 권한 'ALL' = 둘 다 허용, 'NO_ITEM' = ITEM 금지.
"""

import sys

import sqlglot

# 패키지 경로가 다르면 이 줄만 고친다
from sql_rewriter import RewriterRequest, rewrite_sql_request

ALL = ["*"]
NO_ITEM = ["ID"]


def policy(t1=ALL, t2=ALL, t3=ALL):
    def table(allowed):
        return {"all_columns": ["ID", "ITEM"], "allowed_columns": allowed, "row_filter": ""}
    return {"tables": {"T1": table(t1), "T2": table(t2), "T3": table(t3)}}


U = "WITH c AS (SELECT ID, ITEM FROM T1 UNION ALL SELECT ID, ITEM FROM T2) "

CASES = [
    # (id, 설명, policy, sql_query, 메인 쿼리 기대값)
    # ── A. NULL 로 잘못 바뀌던 문제 (모두 허용) ─────────────────────────────
    ("A-1", "UNION 컬럼 조회 (별칭.컬럼)", policy(),
     "WITH c AS (SELECT s.ID AS ID, s.ITEM AS ITEM FROM T1 s UNION ALL SELECT h.ID, h.ITEM FROM T2 h) "
     "SELECT c.ITEM FROM c",
     "SELECT c.ITEM FROM c"),
    ("A-2", "UNION 컬럼 조회 (별칭 없이)", policy(),
     U + "SELECT ITEM FROM c",
     "SELECT ITEM FROM c"),
    ("A-3", "JOIN 조건", policy(),
     U + "SELECT a.ID FROM T1 a JOIN c ON a.ID = c.ID",
     "SELECT a.ID FROM (SELECT ID, ITEM FROM T1) a JOIN c ON a.ID = c.ID"),
    ("A-4", "함수 안 (NVL)", policy(),
     U + "SELECT NVL(c.ITEM, 'x') AS V FROM c",
     "SELECT NVL(c.ITEM, 'x') AS V FROM c"),
    ("A-5", "SELECT * 로 합친 UNION", policy(),
     "WITH c AS (SELECT * FROM T1 UNION ALL SELECT * FROM T2) SELECT c.ITEM FROM c",
     "SELECT c.ITEM FROM c"),
    ("A-6", "MINUS", policy(),
     "WITH c AS (SELECT ID, ITEM FROM T1 MINUS SELECT ID, ITEM FROM T2) SELECT c.ITEM FROM c",
     "SELECT c.ITEM FROM c"),

    # ── B. 일부 테이블만 권한 → 허용 데이터는 조회, IS NULL 은 그대로 ─────────
    ("B-1", "T1 허용 / T2 금지 + IS NULL", policy(t2=NO_ITEM),
     U + "SELECT c.ID FROM c WHERE c.ITEM IS NULL",
     "SELECT c.ID FROM c WHERE c.ITEM IS NULL"),
    ("B-2", "T1 금지 / T2 허용 + IS NULL (순서만 반대)", policy(t1=NO_ITEM),
     U + "SELECT c.ID FROM c WHERE c.ITEM IS NULL",
     "SELECT c.ID FROM c WHERE c.ITEM IS NULL"),
    ("B-3", "T1 금지 / T2 허용 + 조회", policy(t1=NO_ITEM),
     U + "SELECT c.ITEM FROM c",
     "SELECT c.ITEM FROM c"),
    ("B-4", "위아래 컬럼 이름이 다른 UNION", policy(t1=NO_ITEM),
     "WITH c AS (SELECT ITEM AS X FROM T1 UNION ALL SELECT ITEM AS Y FROM T2) "
     "SELECT c.X FROM c WHERE c.X IS NULL",
     "SELECT c.X FROM c WHERE c.X IS NULL"),
    ("B-5", "3중 UNION, 마지막만 허용", policy(t1=NO_ITEM, t2=NO_ITEM),
     "WITH c AS (SELECT ITEM FROM T1 UNION ALL SELECT ITEM FROM T2 UNION ALL SELECT ITEM FROM T3) "
     "SELECT ITEM FROM c WHERE ITEM IS NULL",
     "SELECT ITEM FROM c WHERE ITEM IS NULL"),

    # ── C. 모든 테이블 권한 없음 → 기존 규칙 유지 ──────────────────────────
    ("C-1", "둘 다 금지 + IS NULL", policy(t1=NO_ITEM, t2=NO_ITEM),
     U + "SELECT c.ID FROM c WHERE c.ITEM IS NULL",
     "SELECT c.ID FROM c WHERE 1 = 0"),
    ("C-2", "둘 다 금지 + IS NULL OR 다른 조건", policy(t1=NO_ITEM, t2=NO_ITEM),
     U + "SELECT c.ID FROM c WHERE c.ITEM IS NULL OR c.ID = 1",
     "SELECT c.ID FROM c WHERE 1 = 0 OR c.ID = 1"),

    # ── D. UNION 이 없는 쿼리 → 변화 없음 ─────────────────────────────────
    ("D-1", "CTE, UNION 없음", policy(),
     "WITH c AS (SELECT ID, ITEM FROM T1) SELECT c.ITEM FROM c",
     "SELECT c.ITEM FROM c"),
    ("D-2", "단일 테이블, ITEM 금지 + IS NULL", policy(t1=NO_ITEM),
     "SELECT ID FROM T1 WHERE ITEM IS NULL",
     "SELECT ID FROM (SELECT ID, NULL AS ITEM FROM T1) T1 WHERE 1 = 0"),
]


def main_query(rewritten: str, original: str) -> str:
    """WITH 절 뒤의 메인 쿼리만 잘라낸다 (WITH 가 없으면 전체)."""
    if not original.upper().startswith("WITH"):
        return rewritten
    return rewritten[rewritten.rindex(") SELECT") + 2:]


def main():
    verbose = "-v" in sys.argv
    print(f"sqlglot {sqlglot.__version__}\n")
    failed = 0
    for case_id, title, pol, sql, expected in CASES:
        try:
            out = rewrite_sql_request(
                RewriterRequest(user_id="test", sql=sql, policy=pol)
            ).rewritten_sql
            got = main_query(out, sql)
        except Exception as e:  # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        ok = got == expected
        if not ok:
            failed += 1
        if verbose or not ok:
            print(f"[{'OK  ' if ok else 'FAIL'}] {case_id} {title}")
            print(f"        SQL : {sql}")
            print(f"        기대: {expected}")
            print(f"        결과: {got}\n")
    print(f"{len(CASES)}건 중 {len(CASES) - failed}건 일치, {failed}건 불일치")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
