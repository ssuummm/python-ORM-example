## 대상 시스템
MES

## 시스템별 고위험 키워드 (source=system)
- S-MES-001 | 수율 | 별칭: 양품률, YIELD, YLD | 분류: 공정 | 등급: HIGH | 설명: 제품/LOT/웨이퍼별 수율 값
- S-MES-002 | 불량 | 별칭: DEFECT, 불량률 | 분류: 품질 | 등급: MEDIUM | 설명: 불량 유형·불량률

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- [테이블]: S-MES-001(수율) ← table_name~'YIELD', table_desc~'수율'
- YLD_RT: S-MES-001(수율) ← column_name~'YLD'
- CUST_CD: G-006(고객사) ← column_name~'CUST', column_desc~'고객사', std_term~'고객', std_term_desc~'고객사'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_mes.TB_LOT_YIELD", "system_code": "MES", "table_name": "TB_LOT_YIELD", "table_desc": "LOT별 STEP 수율 실적", "columns": [
  {"name": "LOT_ID", "desc": "LOT 식별자", "std_term": "LOT번호", "type": "VARCHAR(20)", "values": ["L26A00123", "L26A00124"]},
  {"name": "FAB_CD", "desc": "FAB 코드", "std_term": "공장코드", "type": "VARCHAR(8)", "values": ["FAB1", "FAB2"]},
  {"name": "PROD_CD", "desc": "제품코드", "std_term": "제품코드", "type": "VARCHAR(20)", "values": ["DR5-16G-A", "NF7-1T-B"]},
  {"name": "STEP_ID", "desc": "공정 STEP", "std_term": "공정단계ID", "type": "VARCHAR(20)", "values": ["ETCH_0410", "CMP_0520"]},
  {"name": "YLD_RT", "type": "DECIMAL(5,2)", "values": ["99.99"]},
  {"name": "GOOD_DIE_CNT", "desc": "양품 다이 수", "std_term": "양품수량", "type": "INT", "values": ["1834", "1790"]},
  {"name": "CUST_CD", "desc": "고객사 코드", "std_term": "고객코드", "std_term_desc": "거래 고객사 식별 코드", "type": "VARCHAR(10)", "values": ["C0012", "C0045"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
