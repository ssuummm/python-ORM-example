# 고위험 데이터 분류기

전사·시스템별 고위험 키워드를 기준으로, Data Lake 테이블 메타데이터(테이블/컬럼명·설명, 표준 용어, 대표 값)에
민감 데이터가 있는지 LLM으로 판단하고, 민감 테이블에는 고위험 유형(재무성과·영업기밀·사업계획·기술노하우·전략제품)을 붙인다.
설계 배경은 [`docs/DESIGN.md`](docs/DESIGN.md) 참고.

## 구조
```
app.py                      실행 진입점 (python app.py → 로컬 UI) + 파이프라인 run()
ui.py                       Gradio UI: 1 키워드 관리 · 2 프롬프트 편집 · 3 실행 · 결과
crawling.py                 대표 값(샘플 값) 수집용 StarRocks SQL 생성
src/
  config.py                 설정 (환경변수 + UI에서 고른 모델·추론 정도)
  models.py                 입력·LLM 출력·최종 결과 스키마 (pydantic)
  loader.py                 keywords.txt, 크롤링 결과 txt(한글 열 이름) 읽기
  preprocess.py             키워드 사전 매칭, LLM 입력 만들기, 컬럼 분할
  prompt.py                 프롬프트 조립
  prompts/system_prompt.md  민감 판정 프롬프트
  prompts/user_prompt.md    테이블별 사용자 프롬프트 틀
  prompts/tag_prompt.md     고위험 유형 태그 프롬프트
  llm_client.py             openai(OpenAI 호환 Gateway) / anthropic / mock
  parser.py                 응답 JSON 추출·검증
  classifier.py             테이블 1건 처리 (재시도 포함)
  postprocess.py            병합·환각 제거·row 조건 검증·검토 대상 판정
  tagger.py                 민감 테이블의 고위험 유형 태그
  writer.py                 results.jsonl / result_MMDDHHMM.xlsx 저장
```

## 실행
```bash
pip install -r requirements.txt

# LLM 연결 정보는 환경변수로 지정 (코드·UI에 넣지 않는다)
export LLM_PROVIDER=openai LLM_BASE_URL=https://<gateway>/v1 LLM_API_KEY=<key>

python app.py        # 브라우저에 UI가 열린다
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| LLM_PROVIDER | mock | mock / openai / anthropic (mock은 LLM 없이 흐름만 점검) |
| LLM_BASE_URL, LLM_MODEL, LLM_API_KEY | - | 연결 정보 (모델·추론 정도는 UI에서 선택) |
| LLM_JSON_MODE | true | Gateway가 `response_format` 미지원이면 false |

## 입력 (data/ 폴더, git에 올리지 않음)
- `data/keywords.txt`: sys_cd, keyword, description, equivalent_terms(| 구분), high_risk_tag, source, source_detail, history (탭 구분)
- `data/datalake_crawling_result_{스키마}.txt`: 시스템코드, 스키마, 테이블명(물리), 테이블한글명, 원천테이블, 잠금여부, 설명,
  컬럼, 컬럼설명, 동치컬럼, 표준용어, 표준용어내용, 데이터타입, 샘플값(| 구분), 샘플산출방법 (탭 구분)

## 출력 (output/ 폴더, git에 올리지 않음)
- `output/results.jsonl`: 테이블별 상세 결과
- `output/result_MMDDHHMM.xlsx`: 검토용 엑셀 (UI에서 다운로드)
