## 대상 시스템
COM

## 시스템별 고위험 키워드 (source=system)
(없음)

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- CODE_GRP_ID: G-006(고객사) ← value~'CUST'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_com.TB_CMN_CODE", "system_code": "COM", "table_name": "TB_CMN_CODE", "table_desc": "공통 코드 정의", "columns": [
  {"name": "CODE_GRP_ID", "desc": "코드 그룹 ID", "std_term": "코드그룹ID", "type": "VARCHAR(30)", "values": ["XXXX_XXXX", "XXX_XXXXXX", "XXX_XXXX"]},
  {"name": "CODE_ID", "desc": "코드 ID", "std_term": "코드ID", "type": "VARCHAR(30)", "values": ["01", "02", "RUN"]},
  {"name": "CODE_NM", "desc": "코드명", "std_term": "코드명", "type": "VARCHAR(100)", "values": ["양산", "개발", "가동"]},
  {"name": "SORT_SEQ", "desc": "정렬순서", "std_term": "정렬순서", "type": "INT", "values": ["1", "2"]},
  {"name": "USE_YN", "desc": "사용여부", "std_term": "사용여부", "type": "CHAR(1)", "values": ["Y", "N"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
