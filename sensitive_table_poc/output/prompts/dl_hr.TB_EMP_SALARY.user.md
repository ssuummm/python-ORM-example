## 대상 시스템
HR

## 시스템별 고위험 키워드 (source=system)
- S-HR-001 | 징계 | 별칭: DISCIPLINE | 분류: 인사 | 등급: HIGH | 설명: 징계 이력

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- [테이블]: G-002(급여) ← table_name~'SALARY', table_desc~'급여'
- EMP_NM: G-005(성명) ← column_name~'EMP_NM', column_desc~'사원명', std_term~'성명'
- RRN_ENC: G-001(주민등록번호) ← column_name~'RRN', column_desc~'주민등록번호', std_term~'주민등록번호'
- PAY_YM: G-002(급여) ← column_name~'PAY', std_term_desc~'급여'
- SAL_AMT: G-002(급여) ← column_name~'SAL', column_desc~'기본급', std_term~'기본급', std_term_desc~'기본급'
- BONUS_AMT: G-002(급여) ← column_name~'BONUS', column_desc~'상여', std_term~'상여'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_hr.TB_EMP_SALARY", "system_code": "HR", "table_name": "TB_EMP_SALARY", "table_desc": "임직원 월별 급여 지급 내역", "columns": [
  {"name": "EMP_NO", "desc": "사번", "std_term": "사원번호", "std_term_desc": "임직원 고유 식별 번호", "type": "VARCHAR(10)", "values": ["A1023456", "A1034871", "B2001934"]},
  {"name": "EMP_NM", "desc": "사원명", "std_term": "성명", "std_term_desc": "개인의 이름", "type": "VARCHAR(50)", "values": ["가가가"]},
  {"name": "RRN_ENC", "desc": "주민등록번호(암호화)", "std_term": "주민등록번호", "type": "VARCHAR(256)", "values": ["[HASH_OR_ENCRYPTED]"], "value_patterns": ["HASH_OR_ENCRYPTED"]},
  {"name": "DEPT_CD", "desc": "부서코드", "std_term": "부서코드", "std_term_desc": "조직 부서 식별 코드", "type": "VARCHAR(8)", "values": ["D1100", "D2300"]},
  {"name": "PAY_YM", "desc": "지급년월", "std_term": "지급년월", "std_term_desc": "급여 지급 기준 년월(YYYYMM)", "type": "CHAR(6)", "values": ["999999"]},
  {"name": "SAL_AMT", "desc": "월 기본급", "std_term": "기본급액", "std_term_desc": "월 기본급 금액(원)", "type": "DECIMAL(15,0)", "values": ["9999999"]},
  {"name": "BONUS_AMT", "desc": "상여금", "std_term": "상여금액", "type": "DECIMAL(15,0)", "values": ["9", "9999999", "99999999"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
