## 대상 시스템
PMS

## 시스템별 고위험 키워드 (source=system)
- S-PMS-001 | 신제품 | 별칭: 신제품개발, NPI | 분류: 개발 | 등급: HIGH | 설명: 신제품 개발 과제 정보

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- PJT_TYPE_CD: S-PMS-001(신제품) ← std_term_desc~'신제품', value~'NPI'
- SEC_GRADE_CD: G-008(보안등급) ← column_desc~'보안등급', std_term~'보안등급', std_term_desc~'극비'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_pms.TB_PROJECT_MASTER", "system_code": "PMS", "table_name": "TB_PROJECT_MASTER", "table_desc": "연구개발 과제 기준 정보", "columns": [
  {"name": "PJT_CD", "desc": "과제 코드", "std_term": "과제코드", "type": "VARCHAR(20)", "values": ["P26-0012", "P26-0031", "P25-0410"]},
  {"name": "PJT_NM", "desc": "과제명", "std_term": "과제명", "type": "VARCHAR(200)", "values": ["차세대 DRAM 셀 구조 개발", "사내 그룹웨어 고도화", "CMP 공정 개선"]},
  {"name": "PJT_TYPE_CD", "desc": "과제 유형", "std_term": "과제유형코드", "std_term_desc": "NPI:신제품개발, PI:공정개선, IT:IT과제", "type": "VARCHAR(10)", "values": ["NPI", "IT", "PI"]},
  {"name": "SEC_GRADE_CD", "desc": "과제 보안등급", "std_term": "보안등급코드", "std_term_desc": "S:극비, A:대외비, B:사내한정, C:일반", "type": "CHAR(1)", "values": ["S", "C", "A", "B"]},
  {"name": "PM_EMP_NO", "desc": "과제 책임자 사번", "std_term": "사원번호", "type": "VARCHAR(10)", "values": ["A1023456", "A1102938"]},
  {"name": "START_DT", "desc": "시작일자", "std_term": "시작일자", "type": "DATE", "values": ["2026-01-02", "2026-03-15"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
