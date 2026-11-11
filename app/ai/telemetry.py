"""AUSTRO AI - AI run telemetry.

Records one entry per generation in an in-memory ring buffer and emits one
JSON object per run through the ``app.ai.telemetry`` logger. No prompts or
responses are ever stored here, and secrets are never included.
Token/cost fields are populated by the gateway/provider adapter when the
provider reports usage. ``None`` means "not reported"; ``0`` means a measured
zero. The distinction matters: a fabricated zero would look like a free run.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


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
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
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
        tokens_prompt: Optional[int] = None,
        tokens_completion: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
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

        # Log-based metrics: one JSON object per completed run. The standard
        # redacting formatter still processes this record, and no prompt or
        # response text is included.
        logger.info(json.dumps({
            "event": "ai_run",
            "run_id": record.run_id,
            "timestamp": record.timestamp,
            "capability": record.capability,
            "category": record.category,
            "provider": record.provider,
            "model": record.model,
            "latency_ms": record.latency_ms,
            "success": record.success,
            "retry_count": record.retry_count,
            "tokens_prompt": record.tokens_prompt,
            "tokens_completion": record.tokens_completion,
            "estimated_cost_usd": record.estimated_cost_usd,
            "error": record.error,
            "request_id": record.request_id,
            "nonce": record.nonce,
        }, ensure_ascii=False, separators=(",", ":")))
        return record

    def recent(self, limit: Optional[int] = None) -> List[AIRunRecord]:
        with self._lock:
            records = list(self._records)
        return records[-limit:] if limit else records