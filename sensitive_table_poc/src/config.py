"""실행 설정. 환경변수 값을 읽고, UI에서 고른 값(모델·추론 정도)으로 덮어쓴다."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y")


@dataclass
class Settings:
    # --- LLM 연결 ---
    provider: str = "mock"          # mock | openai | anthropic
    base_url: str = ""              # openai: 사내 LLM Gateway(OpenAI 호환) 주소. 예: https://llm-gw.internal/v1
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0        # 분류 작업은 재현성을 위해 0 고정 권장
    max_tokens: int = 4096
    timeout_sec: int = 120
    json_mode: bool = True          # OpenAI 호환 response_format=json_object 사용 여부 (게이트웨이 미지원 시 false)
    reasoning_effort: str = "high"
    max_retries: int = 2            # 파싱/검증 실패 시 재요청 횟수

    # --- 입력 구성 ---
    max_columns_per_call: int = 60  # 컬럼이 많은 테이블은 분할 호출

    # --- 후처리 ---
    review_confidence_threshold: float = 0.7  # 이 값 미만이면 needs_review=true

    prompt_version: str = "v0.1"


def load_settings(**overrides) -> Settings:
    s = Settings(
        provider=os.getenv("LLM_PROVIDER", "mock"),
        base_url=os.getenv("LLM_BASE_URL", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        model=os.getenv("LLM_MODEL", ""),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
        timeout_sec=int(os.getenv("LLM_TIMEOUT_SEC", "120")),
        json_mode=_env_bool("LLM_JSON_MODE", True),
    )
    valid = {f.name for f in fields(Settings)}
    for k, v in overrides.items():
        if v is not None and k in valid:
            setattr(s, k, v)
    return s
