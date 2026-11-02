## 대상 시스템
FAB

## 시스템별 고위험 키워드 (source=system)
- S-FAB-001 | 레시피 | 별칭: 공정조건, RECIPE, RCP | 분류: 공정기술 | 등급: HIGH | 설명: 설비 공정 레시피 및 파라미터 설정값

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- COST_CENTER_CD: G-007(원가) ← column_name~'COST'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_fab.TB_EQP_MASTER", "system_code": "FAB", "table_name": "TB_EQP_MASTER", "table_desc": "설비 기준 정보", "columns": [
  {"name": "EQP_ID", "desc": "설비 ID", "std_term": "설비ID", "type": "VARCHAR(20)", "values": ["EQ-ET-0101", "EQ-CV-0203"]},
  {"name": "EQP_NM", "desc": "설비명", "std_term": "설비명", "type": "VARCHAR(100)", "values": ["ETCH #1", "CVD #3"]},
  {"name": "EQP_MODEL_NM", "desc": "설비 모델명", "std_term": "모델명", "type": "VARCHAR(100)", "values": ["MDL-E300", "MDL-C450"]},
  {"name": "COST_CENTER_CD", "desc": "설비 관리 코스트센터 코드", "std_term": "코스트센터코드", "std_term_desc": "비용 집계 단위 조직 코드", "type": "VARCHAR(10)", "values": ["CC1200", "CC1310"]},
  {"name": "INSTALL_DT", "desc": "설치일자", "std_term": "설치일자", "type": "DATE", "values": ["2023-04-11", "2024-09-02"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
