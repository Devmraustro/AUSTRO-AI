"""AUSTRO AI - AI run telemetry.

Records one line per generation: run id, capability, provider, model,
latency, success, retries and (when safe) the error type. No prompts or
responses are ever stored here, and secrets are never included.
Cost fields (tokens_prompt, tokens_completion, estimated_cost_usd) are
populated by the gateway/provider adapter at record time; they default to
0/0/0.0 so a missing value is distinguishable from a zero-value one.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AIRunRecord:
    run_id: str
    timestamp: float
    capability: str
    category: str
    provider: str
    model: str
    latency_ms: float
    success: bool
    retry_count: int = 0
    error: Optional[str] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    estimated_cost_usd: float = 0.0
    request_id: str = ""
    nonce: Optional[str] = None


class AITelemetry:
    """In-memory ring buffer of AI runs plus a run-id generator."""

    def __init__(self, max_records: int = 200):
        self._records: List[AIRunRecord] = []
        self._max = max_records
        self._lock = threading.Lock()

    def next_run_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def record(
        self,
        run_id: str,
        capability: str,
        category: str,
        provider: str,
        model: str,
        latency_ms: float,
        success: bool,
        retry_count: int = 0,
        error: Optional[str] = None,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        estimated_cost_usd: float = 0.0,
        request_id: str = "",
        nonce: Optional[str] = None,
    ) -> AIRunRecord:
        record = AIRunRecord(
            run_id=run_id,
            timestamp=time.time(),
            capability=capability,
            category=category,
            provider=provider,
            model=model,
            latency_ms=round(latency_ms, 1),
            success=success,
            retry_count=retry_count,
            error=error,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            estimated_cost_usd=estimated_cost_usd,
            request_id=request_id,
            nonce=nonce,
        )
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max:
                self._records = self._records[-self._max:]
        return record

    def recent(self, limit: Optional[int] = None) -> List[AIRunRecord]:
        with self._lock:
            records = list(self._records)
        return records[-limit:] if limit else records