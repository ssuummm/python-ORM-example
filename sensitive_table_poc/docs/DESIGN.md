# Data Lake 민감 테이블 식별 PoC — 입력·출력 설계안

> 모듈 3(Data Platform 통제) 중 Data Lake 테이블의 민감 여부를 LLM으로 1차 식별하는 PoC의 설계 문서.
> 프롬프트 버전: v0.1 (`src/prompts/`)

## 1. 처리 흐름

```
[키워드 사전]  [테이블 메타데이터]
      │              │
      ▼              ▼
 ① 규칙 기반 사전 매칭 (약어 토큰 매칭 포함)  ──┐
 ② 샘플 값 프로파일링·마스킹                      │ 교차검증용
 ③ 컬럼 많은 테이블 분할 (기본 60컬럼)             │
      │                                         │
      ▼                                         │
 ④ LLM 판단 (system: 기준+전사 키워드 / user: 시스템 키워드+힌트+테이블)
      │                                         │
      ▼                                         ▼
 ⑤ 파싱·재시도 → ⑥ 병합·검증·보강 (환각 제거, 조건 검증, 누락 탐지) → results.jsonl / summary.csv
```

LLM 단독 판단이 아니라 **규칙 기반 매칭 → LLM 의미 판단 → 코드 검증**의 3단 구조로 설계했다.
규칙 기반은 재현율(놓치지 않기)을, LLM은 정밀도(글자만 겹치는 오탐 걸러내기, 약어 해석)를, 코드 검증은 신뢰성(환각·형식 오류 차단)을 맡는다.

## 2. Input 설계 — 잘 동작하게 하려면

### 2.1 키워드는 "단어"가 아니라 "개념 사전"으로 넣는다
Data Lake 컬럼명은 `SAL_AMT`, `YLD_RT`, `RRN_ENC`처럼 약어 조합이라, 키워드 단어만 주면 놓치는 경우가 많다.

| 필드 | 예시 | 이유 |
|---|---|---|
| keyword_id | G-002 / S-MES-001 | 출력에서 근거 키워드를 ID로 받아 코드가 검증·집계 |
| keyword | 급여 | 대표 개념 |
| aliases | 연봉\|기본급\|상여\|SALARY\|SAL\|PAY | **가장 효과가 큼.** 표준 약어사전에서 자동 추출 가능 |
| category | 인사 / 개인정보 / 영업비밀 / 공정기술 | 리포트 집계, 통제 정책 분류 |
| level | HIGH / MEDIUM / LOW | 민감도 등급을 LLM이 아니라 **키워드 등급으로 결정**(재현성) |
| description | 임직원 급여·상여 금액 | 범위 경계 명시 (예: "급여 금액"이지 "급여 지급일"은 아님) |

- 전사 키워드는 모든 테이블에 공통 → **시스템 프롬프트**에 넣어 프롬프트 캐싱 대상으로 만든다.
- 시스템 키워드는 **해당 system_code 테이블에만** 사용자 프롬프트로 넣는다 (무관한 키워드는 오탐을 늘린다).

### 2.2 테이블 메타데이터는 컬럼 1줄 JSON으로, 빈 필드는 생략
```json
{"table_id": "dl_mes.TB_LOT_YIELD", "system_code": "MES", "table_name": "...", "table_desc": "...", "columns": [
  {"name": "YLD_RT", "type": "DECIMAL(5,2)", "values": ["99.99"]},
  {"name": "CUST_CD", "desc": "고객사 코드", "std_term": "고객코드", "std_term_desc": "...", "values": ["C0012"]}
]}
```
- 들여쓰기 JSON 대비 토큰 절감, 컬럼 단위 대응이 명확해 LLM이 컬럼명을 정확히 인용한다.
- `data_type`도 함께 넣는다 (DECIMAL → 금액/비율, VARCHAR(256) → 암호화값 추정 근거).
- **표준 용어 설명(std_term_desc)에 코드값 정의가 있으면 반드시 포함** (예: `S:극비, A:대외비`). Row 조건 도출의 핵심 근거다.

### 2.3 샘플 값은 원본 대신 "프로파일"로
샘플 값 자체가 민감정보이므로, LLM에 원본을 보내는 것은 그 자체로 유출 경로가 된다.

| 경우 | 전달 형태 | 예 |
|---|---|---|
| PII 패턴 탐지 | 패턴 태그 + 형태 | `[KR_MOBILE] 999-9999-9999` |
| 해시/암호문 | 태그만 | `[HASH_OR_ENCRYPTED]` |
| 키워드가 걸린 컬럼의 고유값(금액·이름 등) | 형태만 | `9999999`, `가가가` |
| 코드/구분/등급 컬럼 (`_CD`, `_GRADE`, `~코드/등급`) | 원문 유지 | `S`, `A`, `NPI` ← row 조건 근거 |
| 그 외 | 원문(60자 절단) | `ETCH_0410` |

- 사전 매칭은 **원본 값으로 로컬에서** 수행하고, LLM에는 마스킹 결과만 보낸다.
- 샘플 추출(StarRocks) 권장 쿼리 — 무작위 몇 건보다 **최빈값 상위 N개**가 코드 컬럼 판단에 유리하다.
  ```sql
  SELECT col, COUNT(*) cnt FROM db.tbl GROUP BY col ORDER BY cnt DESC LIMIT 10;
  ```
  수만 테이블 전수는 부하가 크므로, 컬럼별 `approx_count_distinct`로 카디널리티를 먼저 보고 저카디널리티(코드성) 컬럼만 최빈값 추출하는 방식을 권장.

### 2.4 사전 매칭 결과는 "힌트"로만 준다
```
- SAL_AMT: G-002(급여) ← column_name~'SAL', column_desc~'기본급'
```
- 힌트가 없으면 LLM이 약어를 놓치고, 힌트를 "정답"처럼 주면 오탐을 그대로 따라간다.
  → 프롬프트에 "참고만 하라, 힌트에 있어도 비민감일 수 있다"를 명시.
- 3자 이하 영문 약어(SAL, YLD, PWD)는 **토큰 단위로만** 매칭해 `SALES`·`PAYLOAD` 등 부분문자열 오탐을 줄였다.

### 2.5 기타
- temperature 0 고정, 테이블 1개 = 1회 호출 (여러 테이블을 묶으면 컬럼 혼동↑).
- 컬럼 60개 초과 시 분할 호출 후 병합 (테이블명·설명은 매 묶음 반복).

## 3. Output 설계 — 초안 대비 보완점

### 3.1 초안
```json
{"decision": "sensitive", "sensitive data": ["컬럼명", "Row 조건"], "reason": "...", "confidence": "0.0~1.0"}
```

### 3.2 보완안
```json
{
  "decision": "sensitive",
  "sensitive_data": [
    {"type": "column", "column_name": "SAL_AMT", "row_condition": null,
     "matched_keyword_ids": ["G-002"], "evidence": ["column_desc", "std_term"],
     "reason": "월 기본급 금액 컬럼으로 '급여'에 해당", "confidence": 0.95},
    {"type": "row_condition", "column_name": "SEC_GRADE_CD", "row_condition": "SEC_GRADE_CD IN ('S','A')",
     "matched_keyword_ids": ["G-008"], "evidence": ["std_term_desc", "value"],
     "reason": "보안등급 S(극비)·A(대외비) 과제 행", "confidence": 0.9}
  ],
  "non_sensitive_candidates": [{"column_name": "COST_CENTER_CD", "matched_keyword_ids": ["G-007"], "reason": "원가가 아닌 조직 코드"}],
  "unlisted_candidates": [],
  "reason": "테이블 전체 판단 요약",
  "confidence": 0.93,
  "needs_review": false,
  "review_reason": ""
}
```

| # | 보완 내용 | 이유 |
|---|---|---|
| 1 | `sensitive data` → `sensitive_data` (snake_case) | 공백 키는 코드·SQL·OPA에서 다루기 불편 |
| 2 | 문자열 배열 → **객체 배열** + `type`(column / row_condition) | 컬럼과 행 조건을 문자열로 섞으면 후속 시스템이 구분 불가 |
| 3 | `row_condition`을 **SQL WHERE 조건식**으로 | StarRocks Row Policy / OPA 데이터 필터로 바로 연결 가능 |
| 4 | `matched_keyword_ids` | 어떤 키워드 때문인지 추적 → 등급 산정, 키워드별 정확도 분석 |
| 5 | `evidence` (근거 필드) | 근거가 컬럼명뿐인지 설명까지 있는지 → 신뢰도·검토 우선순위 |
| 6 | 항목별 `reason`, `confidence` | 테이블 전체가 아니라 컬럼 단위로 검토·승인 |
| 7 | `confidence`를 문자열이 아닌 **숫자**로 + 구간별 기준 명시 | LLM의 자기평가 confidence는 보정이 안 되어 있으므로, 기준표로 일관성 확보 |
| 8 | `non_sensitive_candidates` | "글자는 겹치지만 비민감"으로 판단한 근거를 남김 → 오탐 감사, 판단 누락과 구분 |
| 9 | `unlisted_candidates` | 키워드 사전 밖의 민감 의심 → **키워드 사전 보강 피드백 루프** |
| 10 | `needs_review` + `review_reason` | 이진 판단을 유지하면서 사람 검토 대상만 추려냄 |

### 3.3 LLM이 아니라 코드가 채우는 필드
LLM에게 맡길수록 흔들리는 값은 코드가 결정한다.

| 필드 | 결정 방식 |
|---|---|
| table_id, system_code, meta(모델·프롬프트 버전·처리시각·재시도 횟수) | 코드 |
| sensitivity_level | 매칭된 키워드 `level`의 최댓값 |
| matched_keywords (이름·등급·출처) | keyword_id로 사전에서 조회 |
| needs_review / review_reasons | LLM 요청 + 아래 검증 결과의 합 |
| validation_warnings | 아래 검증에서 자동 수정한 내역 |

### 3.4 후처리 검증 (`src/postprocess.py`)
- 입력에 없는 컬럼명·keyword_id → 제거 + 경고 (환각 방지)
- row_condition: SQL 금지어(`;`, `--`, DROP/UNION 등) 차단, 대상 컬럼 포함 여부, **조건 값이 샘플 값/표준용어 설명에 실제 존재하는지** 확인
- decision ↔ sensitive_data 불일치 시 **sensitive로 보정** (보안 우선)
- 사전 매칭에서 HIGH 키워드가 컬럼명·설명·표준용어에 명확히 걸렸는데 LLM이 민감/비민감 어디에도 언급하지 않으면 → 검토 대상
- confidence < 0.7 → 검토 대상

## 4. 결과 확인 방법 (정답셋 없이)
- `--provider mock`은 규칙 기반 매칭 결과만 돌려준다. 같은 테이블을 LLM으로 돌린 `summary.csv`와 나란히 보면, LLM이 걸러낸 오탐과 새로 잡은 항목을 확인할 수 있다.
  - 샘플에서 규칙 기반이 잘못 잡는 예: `PAY_YM`(급여 지급년월), `COST_CENTER_CD`, `CODE_GRP_ID`(값 `CUST_TYPE`), `PWD_CHG_DT`
  - 규칙 기반이 놓치는 예: `PARAM_VAL`(레시피 설정값, 키워드 표현 없음)
- 검토는 `needs_review=true` 건부터 본다. 검토 결과가 쌓이면 이후 정량 평가용 정답셋으로 활용할 수 있다.

## 5. 확인 필요 사항
| # | 항목 | 영향 |
|---|---|---|
| 1 | 사내 LLM으로 샘플 값 **원본 전송 허용 여부** | 불가 시 현재 마스킹 방식 유지 (`SEND_RAW_VALUES=false`) |
| 2 | 사용할 LLM 엔드포인트·모델 (OpenAI 호환 Gateway 여부) | `LLM_PROVIDER` 설정 |
| 3 | 전사/시스템 키워드 사전의 실제 형식 — 동의어·약어(aliases) 보유 여부 | 없으면 표준 약어사전에서 생성 필요 |
| 4 | 메타데이터 추출 경로 (메타데이터 관리 시스템 export vs StarRocks information_schema) 및 표준용어 매핑 커버리지 | 로더 형식, 설명 누락 테이블 비율 |
| 5 | "민감 테이블"의 판정 단위 — 테이블 단위 통제인지, 컬럼/행 단위까지 통제하는지 | row_condition 결과 활용 범위 |
| 6 | 식별자(사번 등) 단독 컬럼을 민감으로 볼지 (개인정보 결합 가능성) | 프롬프트 판단 절차 2-c 조정 |
| 7 | 판정 결과의 모듈 3 통제 연동 방식 (OPA data.json / StarRocks Row·Column Policy) | 출력 → 정책 데이터 변환기 설계 |
| 8 | 레시피 테이블처럼 "파라미터명"도 민감으로 볼지 등 시스템별 경계 기준 | 시스템 키워드 description 보강 |

## 6. 다음 단계 제안
1. 고객사 실제 키워드 사전·메타데이터 샘플(시스템 2~3개) 확보
2. 실제 LLM으로 1차 실행 → 결과를 담당자와 검토, 오판 사례로 프롬프트 v0.2 개선
3. needs_review 비율이 감당 가능한 수준인지 확인 후 confidence 임계값 조정
4. 결과 → OPA data.json / StarRocks 정책 변환기 설계 (모듈 3 통제 방안과 연계)
