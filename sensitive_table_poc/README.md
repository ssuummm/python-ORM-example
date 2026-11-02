# Data Lake 민감 테이블 식별 PoC

전사·시스템별 고위험 키워드를 기준으로, Data Lake 테이블 메타데이터(테이블/컬럼명·설명, 표준 용어, 샘플 값)에 민감 데이터가 있는지 LLM으로 판단한다.
설계 배경과 입력·출력 보완안은 [`docs/DESIGN.md`](docs/DESIGN.md) 참고.

## 구조
```
app.py                      CLI (run / eval)
src/
  config.py                 설정 (환경변수 + CLI)
  models.py                 입력·LLM 출력·최종 결과 스키마 (pydantic)
  loader.py                 키워드 CSV, 테이블 메타데이터 JSON/CSV 로더
  preprocess.py             키워드 사전 매칭, 샘플 값 마스킹, 컬럼 분할
  prompt.py                 프롬프트 조립
  prompts/system_prompt.md  시스템 프롬프트 (판단 기준·출력 스키마·전사 키워드)
  prompts/user_prompt.md    사용자 프롬프트 (시스템 키워드·힌트·테이블)
  llm_client.py             openai(OpenAI 호환 Gateway) / anthropic / mock
  parser.py                 응답 JSON 추출·검증
  classifier.py             테이블 1건 처리 오케스트레이션 (재시도 포함)
  postprocess.py            병합·환각 제거·row 조건 검증·검토 대상 판정
  writer.py                 results.jsonl / summary.csv / 프롬프트 덤프
  evaluate.py               정답셋 대비 Precision / Recall
sample_data/                반도체 업무 예시 키워드·테이블 7개·정답셋 (예시 데이터)
tests/                      LLM 없이 파싱·검증 로직 테스트
```

## 실행
```bash
pip install -r requirements.txt

# 1) 프롬프트만 생성해 확인 (LLM 호출 없음) → output/prompts/
python app.py run --dry-run

# 2) 규칙 기반 baseline (LLM 없이 파이프라인 점검 + 비교 기준)
python app.py run --provider mock && python app.py eval

# 3) 사내 LLM Gateway (OpenAI 호환)
export LLM_BASE_URL=https://<gateway>/v1  LLM_MODEL=<model>  LLM_API_KEY=<key>
python app.py run --provider openai --workers 4
python app.py eval

# 4) 대량 처리: 중단 후 이어하기, 특정 시스템만
python app.py run --provider openai --tables meta.csv --system MES --resume
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| LLM_PROVIDER | mock | mock / openai / anthropic |
| LLM_BASE_URL, LLM_MODEL, LLM_API_KEY | - | 연결 정보 |
| LLM_JSON_MODE | true | Gateway가 `response_format` 미지원이면 false |
| SEND_RAW_VALUES | false | [확인 필요] 샘플 값 원본 전송 여부 |

## 입력 형식
- 키워드 CSV: `keyword_id, keyword, aliases(|구분), category, level, description` (+ 시스템 키워드는 `system_code`)
- 테이블: `sample_data/tables.json` 또는 컬럼 1행 flat CSV(`sample_data/tables_flat_sample.csv`)

## 출력
- `output/results.jsonl` — 테이블별 상세 결과 (민감 컬럼·row 조건·근거 키워드·검토 사유)
- `output/summary.csv` — 검토자용 요약 (Excel에서 바로 열림)
- `output/eval_report.json` — 정답셋 평가

## 테스트
```bash
python -m pytest -q tests
```
