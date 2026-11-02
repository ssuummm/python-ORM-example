## 대상 시스템
FAB

## 시스템별 고위험 키워드 (source=system)
- S-FAB-001 | 레시피 | 별칭: 공정조건, RECIPE, RCP | 분류: 공정기술 | 등급: HIGH | 설명: 설비 공정 레시피 및 파라미터 설정값

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- [테이블]: S-FAB-001(레시피) ← table_name~'RCP', table_desc~'공정조건'
- RCP_ID: S-FAB-001(레시피) ← column_name~'RCP', column_desc~'레시피', std_term~'레시피'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_fab.TB_RCP_PARAM", "system_code": "FAB", "table_name": "TB_RCP_PARAM", "table_desc": "설비별 공정 조건 파라미터", "columns": [
  {"name": "RCP_ID", "desc": "레시피 ID", "std_term": "레시피ID", "type": "VARCHAR(40)", "values": ["XXXX_XXX_X99", "XXX_XX_X9"]},
  {"name": "EQP_ID", "desc": "설비 ID", "std_term": "설비ID", "type": "VARCHAR(20)", "values": ["EQ-ET-0101", "EQ-CV-0203"]},
  {"name": "PARAM_NM", "desc": "파라미터명", "std_term": "파라미터명", "type": "VARCHAR(50)", "values": ["RF_POWER", "CHAMBER_PRESSURE", "GAS_FLOW_CF4"]},
  {"name": "PARAM_VAL", "desc": "설정값", "std_term": "설정값", "type": "VARCHAR(50)", "values": ["1250", "35.5", "80"]},
  {"name": "UPD_DT", "desc": "수정일시", "std_term": "수정일시", "type": "DATETIME", "values": ["2026-10-28 13:22:01"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
