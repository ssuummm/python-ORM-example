# External Access Check (API / MCP / Skill)

사내망에서 외부 AI 연동(공개 API, 원격 MCP 서버, Agent Skill)이 가능한지 점검하는 Python 스크립트입니다.
Python 표준 라이브러리만 사용하므로 별도 설치 없이 실행할 수 있습니다 (Python 3.8+).

## 점검 대상

| 구분 | 대상 | 내용 |
|---|---|---|
| API | Open-Meteo 예보 / 지오코딩 | 인증 없는 공개 날씨 API, 읽기 전용 |
| MCP | DeepWiki MCP, Microsoft Learn MCP | Streamable HTTP로 `initialize` → `tools/list` → `tools/call` |
| SKILL-1 | GitHub `anthropics/skills` | xlsx Skill의 `SKILL.md` 다운로드 및 파싱 |
| SKILL-2 | Claude API | `ANTHROPIC_API_KEY`가 있으면 Anthropic xlsx Skill 실제 실행 (소량 과금) |

각 대상마다 DNS → TCP → HTTP 순서로 확인하고, 실패 시 원인을 추정합니다.
(DNS 차단, 방화벽 Drop, 프록시 차단 403/407, SSL 검사, IPS 리셋 등)

## 사용법

```bash
python external_access_check.py
python external_access_check.py --only api,mcp
python external_access_check.py --ca-bundle corp-root.pem --report result.json
```

| 옵션 | 설명 |
|---|---|
| `--only` | 점검 범위 (`api`, `mcp`, `skill` 조합) |
| `--timeout` | 요청 타임아웃(초), 기본 15 |
| `--ca-bundle` | SSL 검사 환경의 루트 CA(PEM) 경로 |
| `--report` | 결과를 JSON 파일로 저장 |

환경변수
- `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`: 프록시 설정을 그대로 따릅니다.
- `ANTHROPIC_API_KEY`: 설정 시 SKILL-2(Claude API Skill 실행)까지 점검합니다.
- `ANTHROPIC_MODEL`: SKILL-2에 사용할 모델 (기본 `claude-sonnet-5-5`).

종료 코드: FAIL이 하나라도 있으면 1, 아니면 0.

## 보안 참고

- 사내 데이터는 전송하지 않습니다. 고정된 공개 질의(서울 날씨, 공개 문서 조회 등)만 보냅니다.
- MCP는 운영 주체가 명확하고 인증 없이 공개 문서만 조회하는 서버를 사용합니다.
