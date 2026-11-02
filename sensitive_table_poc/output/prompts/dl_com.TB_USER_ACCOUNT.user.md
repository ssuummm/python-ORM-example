## 대상 시스템
COM

## 시스템별 고위험 키워드 (source=system)
(없음)

## 사전 키워드 매칭 힌트 (규칙 기반 문자열 매칭, 참고용)
- USER_NM: G-005(성명) ← column_name~'USER_NM', column_desc~'사용자명', std_term~'성명'
- EMAIL_ADDR: G-004(연락처) ← column_name~'EMAIL', column_desc~'이메일', std_term~'이메일'
- MOBILE_NO: G-004(연락처) ← column_name~'MOBILE', column_desc~'휴대폰', std_term~'전화번호'
- PWD_HASH: G-003(비밀번호) ← column_name~'PWD', column_desc~'비밀번호', std_term~'비밀번호'
- PWD_CHG_DT: G-003(비밀번호) ← column_name~'PWD', column_desc~'비밀번호'

## 판단 대상 테이블 메타데이터
※ 테이블 전체 컬럼이 제공되었다.
```json
{"table_id": "dl_com.TB_USER_ACCOUNT", "system_code": "COM", "table_name": "TB_USER_ACCOUNT", "table_desc": "시스템 사용자 계정", "columns": [
  {"name": "USER_ID", "desc": "사용자 ID", "std_term": "사용자ID", "type": "VARCHAR(30)", "values": ["mskim", "syl.lee"]},
  {"name": "USER_NM", "desc": "사용자명", "std_term": "성명", "type": "VARCHAR(50)", "values": ["가가가"]},
  {"name": "EMAIL_ADDR", "desc": "이메일 주소", "std_term": "이메일주소", "type": "VARCHAR(100)", "values": ["[EMAIL] XXXXX@XXXXXXX.XXX", "[EMAIL] XXX.XXX@XXXXXXX.XXX"], "value_patterns": ["EMAIL"]},
  {"name": "MOBILE_NO", "desc": "휴대폰번호", "std_term": "휴대전화번호", "type": "VARCHAR(20)", "values": ["[KR_MOBILE] 999-9999-9999"], "value_patterns": ["KR_MOBILE"]},
  {"name": "PWD_HASH", "desc": "비밀번호 해시", "std_term": "비밀번호", "type": "VARCHAR(256)", "values": ["[HASH_OR_ENCRYPTED]"], "value_patterns": ["HASH_OR_ENCRYPTED"]},
  {"name": "PWD_CHG_DT", "desc": "비밀번호 변경일자", "std_term": "변경일자", "type": "DATE", "values": ["9999-99-99"]},
  {"name": "LAST_LOGIN_DT", "desc": "최종 로그인 일시", "std_term": "최종로그인일시", "type": "DATETIME", "values": ["2026-11-01 08:51:22"]}
]}
```

위 테이블에 민감 데이터가 포함되어 있는지 시스템 프롬프트의 판단 절차에 따라 판단하고, 지정된 JSON 스키마의 객체 하나만 출력하라.
