"""LLM 응답 텍스트 → LLMDecision."""
from __future__ import annotations

import json
import re

from .models import LLMDecision

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> dict:
    text = _FENCE.sub("", (text or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("응답에서 JSON 객체를 찾지 못함")
    return json.loads(text[start:end + 1])


def parse_decision(text: str) -> LLMDecision:
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("최상위가 JSON 객체가 아님")
    return LLMDecision.model_validate(data)
