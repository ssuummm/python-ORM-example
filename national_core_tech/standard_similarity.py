"""
국가핵심기술 키워드 ↔ 사내 표준(단어/도메인/용어) 임베딩 유사도 분석

목표
    국가핵심기술 키워드가 사내 표준 단어/도메인/용어에서 어떻게 표현되는지 찾는다.

방법
    1. 국가핵심기술 키워드 리스트를 만든다. (L5 + keyword 합집합, ',' 기준 분리, 중복 제거)
       → 키워드 임베딩은 노트북에서 저장해 둔 keyword_emb.npy 를 그대로 사용
    2. 표준 파일마다 지정한 컬럼들을 각각 임베딩한다.
       → 한 번 임베딩한 결과는 emb_cache 폴더에 저장해 두고 다음 실행 때 재사용
    3. 키워드와 각 컬럼의 코사인 유사도를 구하고,
       컬럼 중 하나라도 THRESHOLD 이상이면 결과에 남긴다.
    4. 표준 파일별로 엑셀 파일을 하나씩 저장한다.

실행
    python standard_similarity.py
"""

import json
import os
import time

import numpy as np
import pandas as pd
import requests
from tqdm import tqdm


# =============================================================================
# 1. 설정
# =============================================================================

# 사내 LLM API (주소와 키는 환경변수로 설정)
BASE_URL = os.environ.get("LLM_BASE_URL", "http://<사내 LLM 주소>/v1")
EMBEDDING_URL = f"{BASE_URL}/embeddings"
OPEN_API_KEY = os.environ.get("LLM_API_KEY", "<사내 API 키>")
MODEL_NAME = "bge-m3"

HEADERS = {
    "Authorization": f"Bearer {OPEN_API_KEY}",
    "Content-Type": "application/json",
}

# 유사도 기준값
THRESHOLD = 0.5

# 국가핵심기술 키워드 파일과, 노트북에서 저장해 둔 키워드 임베딩
KEYWORD_FILE = "국가핵심기술_키워드.xlsx"   # 컬럼: L5, keyword
KEYWORD_EMB_FILE = "keyword_emb.npy"

# 표준 파일별 임베딩 캐시를 저장할 폴더
CACHE_DIR = "emb_cache"

# 분석할 표준 파일 목록 (탭 구분 txt 파일, 파일 경로는 실제에 맞게 수정)
#   - columns: 키워드와 유사도를 비교할 컬럼 (결과 엑셀에도 이 순서대로 나옴)
#   - output : 결과 엑셀 파일 이름
TARGETS = {
    "표준단어": {
        "file": "data/smartda_표준단어.txt",
        "columns": ["논리명", "물리명", "물리의미", "설명"],
        "output": "결과_표준단어_유사도.xlsx",
    },
    "표준도메인": {
        "file": "data/smartda_표준도메인.txt",
        "columns": ["도메인명", "설명"],
        "output": "결과_표준도메인_유사도.xlsx",
    },
    "표준용어": {
        "file": "data/smartda_표준용어.txt",
        "columns": ["논리명", "물리명", "구성정보", "물리의미", "도메인명", "설명"],
        "output": "결과_표준용어_유사도.xlsx",
    },
}


# =============================================================================
# 2. 공통 함수
# =============================================================================

def read_table(path):
    """확장자에 따라 txt(탭 구분), CSV, 엑셀 파일을 읽는다."""
    if path.endswith(".txt"):
        return pd.read_csv(path, sep="\t")
    if path.endswith(".csv"):
        return pd.read_csv(path)
    return pd.read_excel(path)


def make_keyword_list(df_keyword):
    """
    L5 열과 keyword 열을 합쳐 하나의 키워드 리스트로 만든다.
    (노트북과 똑같은 순서로 만들어야 저장된 keyword_emb.npy 와 순서가 맞는다)
    """
    # L5 값 자체도 키워드로 사용 → (L5, 값) 형태로 세로로 쌓기
    df_from_l5 = df_keyword[["L5"]].copy()
    df_from_l5["keyword_raw"] = df_keyword["L5"]

    df_from_kw = df_keyword[["L5"]].copy()
    df_from_kw["keyword_raw"] = df_keyword["keyword"]

    df_kw_long = pd.concat([df_from_l5, df_from_kw], ignore_index=True)
    df_kw_long = df_kw_long.dropna(subset=["keyword_raw"])

    # ',' 기준으로 쪼개서 한 행에 하나의 키워드만 남기기
    df_kw_long["keyword"] = df_kw_long["keyword_raw"].astype(str).str.split(",")
    df_kw_long = df_kw_long.explode("keyword")

    # 공백 제거 후 빈 문자열 제거
    df_kw_long["keyword"] = df_kw_long["keyword"].str.strip()
    df_kw_long = df_kw_long[df_kw_long["keyword"] != ""]

    df_kw_long = df_kw_long[["L5", "keyword"]].drop_duplicates().reset_index(drop=True)

    # 고유 키워드 리스트
    return df_kw_long["keyword"].drop_duplicates().tolist()


def get_embeddings(texts, batch_size=16, sleep_sec=0.5, max_retry=5):
    """texts 리스트를 batch_size개씩 임베딩. 429가 오면 기다렸다가 다시 시도"""
    all_vectors = []

    for i in tqdm(range(0, len(texts), batch_size)):
        batch = texts[i : i + batch_size]
        body = {"input": batch, "model": MODEL_NAME}

        for attempt in range(max_retry):
            rsp = requests.post(EMBEDDING_URL, headers=HEADERS, json=body, timeout=60)

            if rsp.status_code == 429:
                # 서버가 기다릴 시간을 알려주면 그만큼, 아니면 2, 4, 8, ...초씩 늘려가며 대기
                wait = int(rsp.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"429 발생 → {wait}초 대기 후 재시도 ({attempt + 1}/{max_retry})")
                time.sleep(wait)
                continue

            rsp.raise_for_status()
            break
        else:
            raise RuntimeError(f"{max_retry}번 재시도했지만 계속 429: batch 시작 위치 {i}")

        data = sorted(rsp.json()["data"], key=lambda x: x["index"])
        all_vectors.extend([d["embedding"] for d in data])

        time.sleep(sleep_sec)   # 배치 사이에 쉬어서 한도 초과를 예방

    return np.array(all_vectors)


def normalize(x):
    """벡터 길이를 1로 맞춘다. (정규화된 벡터끼리 내적 = 코사인 유사도)"""
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def get_embeddings_with_cache(texts, cache_name):
    """
    texts 를 임베딩한다.
    같은 texts 로 저장해 둔 캐시가 있으면 API를 호출하지 않고 불러온다.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    emb_path = os.path.join(CACHE_DIR, f"{cache_name}.npy")
    text_path = os.path.join(CACHE_DIR, f"{cache_name}_texts.json")

    # 캐시가 있고, 저장된 텍스트가 지금 텍스트와 똑같으면 재사용
    if os.path.exists(emb_path) and os.path.exists(text_path):
        with open(text_path, encoding="utf-8") as f:
            cached_texts = json.load(f)
        if cached_texts == texts:
            print(f"  캐시 사용: {emb_path}")
            return np.load(emb_path)
        print("  데이터가 바뀌어 다시 임베딩합니다.")

    emb = get_embeddings(texts)

    np.save(emb_path, emb)
    with open(text_path, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False)

    return emb


def column_similarity(df, col, keyword_emb_norm, cache_name):
    """
    df[col] 의 각 행과 모든 키워드의 유사도를 구한다.
    반환값 shape: (키워드 수, 행 수) / 값이 비어 있는 행은 NaN
    """
    values = df[col].fillna("").astype(str).str.strip()

    # 같은 값이 여러 행에 있을 수 있으므로, 고유한 값만 임베딩해서 API 호출을 줄인다
    unique_texts = [v for v in values.unique() if v != ""]
    print(f"  [{col}] 고유 값 {len(unique_texts)}개 임베딩")

    unique_emb = get_embeddings_with_cache(unique_texts, cache_name)
    unique_sim = keyword_emb_norm @ normalize(unique_emb).T   # (키워드 수, 고유 값 수)

    # 고유 값 기준 유사도를 원래 행 순서로 다시 펼치기
    text_to_pos = {text: pos for pos, text in enumerate(unique_texts)}

    sim = np.full((len(keyword_emb_norm), len(df)), np.nan)
    for row, text in enumerate(values):
        if text != "":
            sim[:, row] = unique_sim[:, text_to_pos[text]]

    return sim


# =============================================================================
# 3. 표준 파일 하나 분석
# =============================================================================

def analyze_target(name, target, keyword_list, keyword_emb_norm):
    print(f"\n===== {name} =====")

    df = read_table(target["file"]).reset_index(drop=True)
    columns = target["columns"]
    print(f"행 수: {len(df)}")

    # 1) 컬럼별 유사도 행렬 구하기 → {컬럼명: (키워드 수, 행 수) 배열}
    sims = {}
    for col in columns:
        sims[col] = column_similarity(df, col, keyword_emb_norm, cache_name=f"{name}_{col}")

    # 2) 컬럼 중 하나라도 THRESHOLD 이상인 (키워드, 행) 찾기
    is_matched = np.zeros((len(keyword_list), len(df)), dtype=bool)
    for col in columns:
        is_matched |= np.nan_to_num(sims[col], nan=-1) >= THRESHOLD

    kw_idx, row_idx = np.where(is_matched)
    print(f"매칭 건수: {len(kw_idx)}")

    # 3) 결과표 만들기: keyword, 컬럼1, 컬럼1_similarity, 컬럼2, 컬럼2_similarity, ...
    df_result = pd.DataFrame({"keyword": [keyword_list[k] for k in kw_idx]})

    for col in columns:
        df_result[col] = df[col].iloc[row_idx].values
        df_result[f"{col}_similarity"] = sims[col][kw_idx, row_idx].round(4)

    # 키워드별로, 가장 높은 유사도 순으로 정렬
    sim_cols = [f"{col}_similarity" for col in columns]
    df_result["_max"] = df_result[sim_cols].max(axis=1)
    df_result = (
        df_result.sort_values(["keyword", "_max"], ascending=[True, False])
        .drop(columns="_max")
        .reset_index(drop=True)
    )

    # 4) 엑셀로 저장 (엑셀은 최대 약 104만 행까지만 저장 가능)
    if len(df_result) > 1_000_000:
        print("결과가 너무 많아 CSV로 저장합니다. THRESHOLD를 올리는 것을 권장합니다.")
        df_result.to_csv(target["output"].replace(".xlsx", ".csv"), index=False, encoding="utf-8-sig")
    else:
        df_result.to_excel(target["output"], index=False)

    print(f"저장 완료: {target['output']}")
    return df_result


# =============================================================================
# 4. 실행
# =============================================================================

if __name__ == "__main__":
    # 키워드 리스트 만들고, 저장해 둔 키워드 임베딩 불러오기
    df_keyword = read_table(KEYWORD_FILE)
    keyword_list = make_keyword_list(df_keyword)
    keyword_emb = np.load(KEYWORD_EMB_FILE)

    assert len(keyword_emb) == len(keyword_list), (
        f"키워드 수({len(keyword_list)})와 임베딩 수({len(keyword_emb)})가 다릅니다. "
        "키워드 파일이 바뀌었다면 키워드 임베딩을 다시 만들어야 합니다."
    )
    print(f"키워드 {len(keyword_list)}개, 임베딩 불러오기 완료")

    keyword_emb_norm = normalize(keyword_emb)

    # 표준 파일별로 분석
    for name, target in TARGETS.items():
        analyze_target(name, target, keyword_list, keyword_emb_norm)

    print("\n모든 분석 완료")
