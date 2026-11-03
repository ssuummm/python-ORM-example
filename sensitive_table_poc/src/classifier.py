"""테이블 1건 판단 오케스트레이션: 사전매칭 → 프롬프트 → LLM → 파싱/재시도 → 병합/검증."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from .config import Settings
from .llm_client import LLMClient
from .models import TABLE_TARGET, Keyword, KeywordHit, LLMDecision, TableMeta, TableResult
from .parser import parse_decision
from .postprocess import finalize, merge_decisions
from .preprocess import KeywordMatcher, build_table_payload, chunk_table, format_hints
from .prompt import RETRY_SUFFIX, build_system_prompt, build_user_prompt

log = logging.getLogger(__name__)


class SensitiveTableClassifier:
    def __init__(self, global_kws: list[Keyword], system_kws: list[Keyword],
                 settings: Settings, client: LLMClient | None):
        self.global_kws = global_kws
        self.system_kws = system_kws
        self.s = settings
        self.client = client
        self.system_prompt = build_system_prompt(global_kws)  # 전 테이블 공통

    # ------------------------------------------------------------------
    def _prepare(self, table: TableMeta):
        sys_kws = [k for k in self.system_kws if k.sys_cd == table.sys_cd]
        kws = self.global_kws + sys_kws
        kw_index = {k.keyword_id: k for k in kws}
        hits = KeywordMatcher(kws).find_hits(table)
        prompts = []
        chunks = chunk_table(table, self.s.max_columns_per_call)
        for n, ch in enumerate(chunks, 1):
            names = {c.column_name for c in ch.columns}
            ch_hits = [h for h in hits if h.target == TABLE_TARGET or h.target in names]
            payload = build_table_payload(ch)
            user = build_user_prompt(table.sys_cd, sys_kws, payload,
                                     format_hints(ch_hits, kw_index), n, len(chunks))
            prompts.append((ch, ch_hits, user))
        return kw_index, hits, prompts

    def render_prompts(self, table: TableMeta) -> list[str]:
        """dry-run: LLM 호출 없이 실제 전송될 사용자 프롬프트 확인."""
        return [u for _, _, u in self._prepare(table)[2]]

    # ------------------------------------------------------------------
    def _call(self, user: str, context: dict) -> tuple[LLMDecision, int]:
        prompt, last_err = user, None
        for attempt in range(self.s.max_retries + 1):
            raw = self.client.complete(self.system_prompt, prompt, context)
            try:
                return parse_decision(raw), attempt
            except (ValueError, ValidationError) as e:
                last_err = str(e).replace("\n", " ")[:400]
                log.warning("파싱 실패(%d회차): %s", attempt + 1, last_err)
                prompt = user + RETRY_SUFFIX.format(error=last_err)
        raise ValueError(f"재시도 후에도 응답 파싱 실패: {last_err}")

    def classify(self, table: TableMeta) -> TableResult:
        t0 = time.time()
        meta = {"provider": self.s.provider, "model": self.s.model,
                "prompt_version": self.s.prompt_version,
                "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        hits: list[KeywordHit] = []
        try:
            kw_index, hits, prompts = self._prepare(table)
            decisions, retries = [], 0
            for ch, ch_hits, user in prompts:
                d, r = self._call(user, {"table": ch, "hits": ch_hits, "kw_index": kw_index})
                decisions.append(d)
                retries += r
            meta.update(chunks=len(prompts), retries=retries,
                        latency_ms=int((time.time() - t0) * 1000))
            return finalize(table, merge_decisions(decisions), kw_index, hits, self.s, meta)
        except Exception as e:  # 한 테이블 실패가 전체 배치를 멈추지 않도록
            log.exception("판단 실패: %s", table.table_id)
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            return TableResult(table_id=table.table_id, sys_cd=table.sys_cd,
                               decision="error", needs_review=True,
                               review_reasons=["처리 오류"], prefilter_hit_count=len(hits),
                               meta=meta, error=f"{type(e).__name__}: {e}")