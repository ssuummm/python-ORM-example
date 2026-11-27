"""
국가핵심기술 키워드 파일에 유사어(equivalent_words, equivalent_terms, total) 붙이기

입력
    - 국가핵심기술 키워드 파일 (L1, L2, L3, L4, L5, keyword, equivalent_words, equivalent_terms)
    - standard_similarity.py 결과 파일
        · 결과_표준단어_유사도.xlsx → equivalent_words
        · 결과_표준용어_유사도.xlsx → equivalent_terms

방법
    키워드 파일의 행마다
    1. L5 와 keyword 값을 ',' 기준으로 쪼개서 그 행의 키워드 목록을 만든다.
    2. 결과 파일에서 keyword 가 이 목록에 들어 있는 행을 찾는다.
    3. 찾은 행의 "물리명"을 중복 없이 ', ' 로 이어 붙인다. (유사도가 높은 것부터)
    4. equivalent_words 와 equivalent_terms 를 합쳐 중복 없이 total 에 넣는다.

실행
    python build_equivalents.py
"""

import os

import pandas as pd

# 키워드 파일 경로, 결과 파일 경로, 파일 읽는 함수는 standard_similarity.py 의 것을 그대로 사용
from standard_similarity import KEYWORD_FILE, TARGETS, read_table


# =============================================================================
# 1. 설정
# =============================================================================

WORD_RESULT_FILE = TARGETS["표준단어"]["output"]   # 결과_표준단어_유사도.xlsx
TERM_RESULT_FILE = TARGETS["표준용어"]["output"]   # 결과_표준용어_유사도.xlsx

OUTPUT_FILE = "결과_국가핵심기술_유사어.xlsx"

SEP = ", "   # 유사어를 이어 붙일 때 쓰는 구분자


# =============================================================================
# 2. 함수
# =============================================================================

def read_result(path):
    """결과 파일 읽기 (결과가 많아 CSV로 저장된 경우도 처리)"""
    csv_path = path.replace(".xlsx", ".csv")
    if not os.path.exists(path) and os.path.exists(csv_path):
        return pd.read_csv(csv_path)
    return read_table(path)


def split_keywords(*values):
    """여러 칸의 값을 ',' 기준으로 쪼개서 키워드 목록으로 만든다. (빈 값, 공백 제거)"""
    keywords = []
    for value in values:
        if pd.isna(value):
            continue
        for kw in str(value).split(","):
            kw = kw.strip()
            if kw != "":
                keywords.append(kw)
    return keywords


def unique_in_order(items):
    """순서를 유지하면서 중복 제거"""
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def make_keyword_to_names(df_result):
    """
    결과 파일을 {키워드: [물리명, 물리명, ...]} 형태로 정리한다.
    물리명은 유사도가 높은 순서로, 중복 없이 담는다.
    """
    df = df_result.dropna(subset=["물리명"]).copy()
    df["물리명"] = df["물리명"].astype(str).str.strip()
    df = df[df["물리명"] != ""]

    # 행마다 가장 높은 유사도 (컬럼 중 최댓값)를 기준으로 정렬
    sim_cols = [c for c in df.columns if c.endswith("_similarity")]
    df["max_similarity"] = df[sim_cols].max(axis=1)
    df = df.sort_values(["keyword", "max_similarity"], ascending=[True, False])

    keyword_to_names = {}
    for keyword, group in df.groupby("keyword"):
        keyword_to_names[keyword] = unique_in_order(group["물리명"].tolist())

    return keyword_to_names


def collect_names(keywords, keyword_to_names):
    """키워드 목록에 해당하는 물리명을 모두 모아 중복 없이 반환"""
    names = []
    for kw in keywords:
        names.extend(keyword_to_names.get(kw, []))
    return unique_in_order(names)


# =============================================================================
# 3. 실행
# =============================================================================

if __name__ == "__main__":
    df_keyword = read_table(KEYWORD_FILE)
    df_word_result = read_result(WORD_RESULT_FILE)
    df_term_result = read_result(TERM_RESULT_FILE)

    print(f"키워드 파일     : {len(df_keyword)}행")
    print(f"표준단어 결과   : {len(df_word_result)}행")
    print(f"표준용어 결과   : {len(df_term_result)}행")

    word_map = make_keyword_to_names(df_word_result)
    term_map = make_keyword_to_names(df_term_result)

    equivalent_words = []
    equivalent_terms = []
    totals = []

    for _, row in df_keyword.iterrows():
        # 이 행의 키워드 목록 (L5 + keyword)
        keywords = split_keywords(row["L5"], row["keyword"])

        words = collect_names(keywords, word_map)
        terms = collect_names(keywords, term_map)
        total = unique_in_order(words + terms)

        equivalent_words.append(SEP.join(words))
        equivalent_terms.append(SEP.join(terms))
        totals.append(SEP.join(total))

    # 기존 equivalent_words / equivalent_terms 열은 새 결과로 덮어쓴다
    df_keyword["equivalent_words"] = equivalent_words
    df_keyword["equivalent_terms"] = equivalent_terms
    df_keyword["total"] = totals

    df_keyword.to_excel(OUTPUT_FILE, index=False)

    filled = (df_keyword["total"] != "").sum()
    print(f"\n유사어가 1개 이상 붙은 행: {filled} / {len(df_keyword)}")
    print(f"저장 완료: {OUTPUT_FILE}")
