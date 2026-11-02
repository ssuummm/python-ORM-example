"""LLM 클라이언트.

- openai    : OpenAI 호환 /chat/completions (사내 LLM Gateway, vLLM, Azure 프록시 등)
- anthropic : Anthropic Messages API
- mock      : 규칙 기반 가짜 응답. LLM 없이 파이프라인을 점검하기 위한 용도이며 판단 품질과 무관
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any

import requests

from .config import Settings
from .models import TABLE_TARGET


class LLMError(RuntimeError):
    pass


class LLMClient(ABC):
    name = "base"

    def __init__(self, settings: Settings):
        self.s = settings

    @abstractmethod
    def complete(self, system: str, user: str, context: dict[str, Any] | None = None) -> str: ...

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        last = None
        for attempt in range(3):  # 네트워크/5xx/429 재시도
            try:
                r = requests.post(url, headers=headers, json=body, timeout=self.s.timeout_sec)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                return r.json()
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2 ** attempt)
        raise LLMError(f"LLM 호출 실패: {last}")


class OpenAICompatibleClient(LLMClient):
    name = "openai"

    def complete(self, system, user, context=None):
        if not (self.s.base_url and self.s.model):
            raise LLMError("LLM_BASE_URL, LLM_MODEL 환경변수를 설정하세요.")
        body = {
            "model": self.s.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.s.temperature,
            "max_tokens": self.s.max_tokens,
        }
        if self.s.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.s.api_key:
            headers["Authorization"] = f"Bearer {self.s.api_key}"
        data = self._post(self.s.base_url.rstrip("/") + "/chat/completions", headers, body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"예상치 못한 응답 형식: {json.dumps(data)[:300]}") from e


class AnthropicClient(LLMClient):
    name = "anthropic"

    def complete(self, system, user, context=None):
        if not (self.s.api_key and self.s.model):
            raise LLMError("LLM_API_KEY, LLM_MODEL 환경변수를 설정하세요.")
        base = (self.s.base_url or "https://api.anthropic.com").rstrip("/")
        body = {
            "model": self.s.model,
            "max_tokens": self.s.max_tokens,
            "temperature": self.s.temperature,
            # 전사 키워드가 들어간 시스템 프롬프트는 모든 테이블에서 동일 → 캐싱
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self.s.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        data = self._post(base + "/v1/messages", headers, body)
        try:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"예상치 못한 응답 형식: {json.dumps(data)[:300]}") from e


class MockClient(LLMClient):
    """사전 매칭 결과를 그대로 민감 판정으로 돌려주는 가짜 LLM (오프라인 테스트용)."""
    name = "mock"

    def complete(self, system, user, context=None):
        ctx = context or {}
        groups = defaultdict(list)
        for h in ctx.get("hits", []):
            if h.target != TABLE_TARGET:
                groups[h.target].append(h)
        items = []
        for col, hs in groups.items():
            fields = sorted({h.field for h in hs})
            strong = any(f in ("column_desc", "std_term") for f in fields)
            items.append({
                "type": "column", "column_name": col,
                "matched_keyword_ids": sorted({h.keyword_id for h in hs}),
                "evidence": fields,
                "reason": "[MOCK] 규칙 기반 키워드 매칭 결과",
                "confidence": 0.85 if strong else 0.6,
            })
        return json.dumps({
            "decision": "sensitive" if items else "non-sensitive",
            "sensitive_data": items,
            "non_sensitive_candidates": [],
            "unlisted_candidates": [],
            "reason": "[MOCK] 실제 LLM 판단이 아닌 사전 매칭 결과입니다.",
            "confidence": 0.75 if items else 0.6,
            "needs_review": False,
            "review_reason": "",
        }, ensure_ascii=False)


def create_client(settings: Settings) -> LLMClient:
    clients = {"openai": OpenAICompatibleClient, "anthropic": AnthropicClient, "mock": MockClient}
    if settings.provider not in clients:
        raise ValueError(f"알 수 없는 provider: {settings.provider} ({', '.join(clients)})")
    return clients[settings.provider](settings)
