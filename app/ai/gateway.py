"""
AUSTRO AI - AI Gateway.

Routes generation by capability (chat/coaching/tutoring/planning/...), never
by provider. Enforces bounded timeouts, retries with exponential backoff, a
graceful local fallback and per-run telemetry. The gateway never raises raw
networking exceptions into the update loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.ai.gemini import GeminiProvider
from app.ai.local import LocalProvider
from app.ai.telemetry import AITelemetry
from app.config.settings import Settings
from app.core.errors import AIProviderError, RateLimitError
from app.domain.ai import AIRequest, AIResponse, UNSUPPORTED_CAPABILITIES

logger = logging.getLogger(__name__)

_UNAVAILABLE_MESSAGE = "عذراً، الخدمة غير متاحة حالياً. حاول لاحقاً."


class AIGateway:
    """Capability-routed async AI gateway."""

    def __init__(
        self,
        settings_: Settings,
        provider: Optional[GeminiProvider] = None,
        local: Optional[LocalProvider] = None,
        telemetry: Optional[AITelemetry] = None,
    ):
        self._settings = settings_
        self.provider = provider or GeminiProvider(settings_)
        self.local = local or LocalProvider()
        self.telemetry = telemetry or AITelemetry()

    def _fallback_advice(self, request: AIRequest) -> str:
        """Produce a capability-aware fallback when even local AI has nothing."""
        if request.capability in UNSUPPORTED_CAPABILITIES:
            return "⚠️ هذه الميزة غير متاحة حالياً."
        return _UNAVAILABLE_MESSAGE

    async def generate(self, request: AIRequest) -> AIResponse:
        run_id = self.telemetry.next_run_id()
        started = time.perf_counter()
        category = request.category()
        attempt = 0
        last_error: Optional[BaseException] = None

        if category in UNSUPPORTED_CAPABILITIES:
            return self._record(
                run_id, request, category, "none", started, 0,
                self._fallback_advice(request), False, "unsupported capability",
            )

        max_retries = max(0, self._settings.ai_max_retries)
        while attempt <= max_retries:
            if not self.provider.is_available:
                last_error = AIProviderError("AI provider currently unavailable")
                break

            try:
                text = await asyncio.wait_for(
                    self.provider.generate(request),
                    timeout=self._settings.ai_request_timeout,
                )
                return self._record(
                    run_id, request, category, self.provider.name,
                    started, attempt, text, True, None,
                )
            except RateLimitError as exc:
                logger.warning(f"Rate limit hit for {request.capability}: {exc}. Using local AI.")
                last_error = exc
                break
            except (AIProviderError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                attempt += 1
                backoff = self._settings.ai_retry_backoff_base * (2 ** attempt)
                logger.info(
                    f"AI attempt {attempt}/{max_retries} failed for "
                    f"'{request.capability}' ({(time.perf_counter() - started) * 1000:.0f}ms); "
                    f"retrying in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)

        if self._settings.use_local_fallback:
            try:
                text = self.local.generate(request)
                return self._record(
                    run_id, request, category, self.local.name,
                    started, attempt, text, True, None,
                )
            except Exception as exc:  # noqa: BLE001 - fallback must never crash the loop
                last_error = exc
                logger.error(f"Local fallback failed for {request.capability}: {exc}")

        error_text = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown"
        return self._record(
            run_id, request, category, self.provider.name, started, attempt,
            self._fallback_advice(request), False, error_text,
        )

    def _record(
        self,
        run_id: str,
        request: AIRequest,
        category: str,
        provider: str,
        started: float,
        retry_count: int,
        text: str,
        success: bool,
        error: Optional[str],
    ) -> AIResponse:
        latency_ms = (time.perf_counter() - started) * 1000
        model = self.provider.model if provider == self.provider.name else "fallback"
        metadata = self.telemetry.record(
            run_id=run_id,
            capability=request.capability,
            category=category,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            success=success,
            retry_count=retry_count,
            error=error,
        )
        return AIResponse(text=text, metadata=metadata)

    def status_text(self) -> str:
        """Arabic quota status (mirrors the original settings view)."""
        if not self.provider.is_available:
            return "⚠️ Gemini غير متاح - يستخدم الردود المحلية"
        remaining = self._settings.max_requests_per_day - self.provider.request_count
        percentage = (remaining / self._settings.max_requests_per_day) * 100
        return (
            f"📊 حالة الاستخدام: {self.provider.request_count}/"
            f"{self._settings.max_requests_per_day} ({percentage:.0f}% متبقي)"
        )

    async def reconnect(self) -> str:
        """Re-run the health check and reset the quota counter on success."""
        await asyncio.to_thread(self.provider.reconnect)
        return self.status_text()