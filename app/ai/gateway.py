"""
AUSTRO AI - AI Gateway.

Routes generation by capability (chat/coaching/tutoring/planning/...), never
by provider. Enforces bounded timeouts, retries with exponential backoff, a
OpenRouter primary provider with a graceful local fallback, a graceful local
fallback and per-run telemetry. The gateway never raises raw networking
exceptions into the update loop.

Failover rules:
  * Providers are tried in order; a provider is skipped entirely when it is
    unconfigured/unavailable.
  * Rate-limit (quota/429) and authentication (401/403) failures are NOT
    retried: the same key cannot succeed on a retry, so the gateway moves to
    the next provider immediately.
  * Transient failures (timeouts, 5xx, connection resets, malformed/empty
    responses) are retried up to `ai_max_retries` with exponential backoff
    before the next provider is tried.
  * When every cloud provider fails, the local fallback answers (if enabled);
    otherwise the caller receives the safe built-in response. Local canned
    answers are always attributed to provider "local" in telemetry - never
    reported as an LLM-generated response.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from app.ai.local import LocalProvider
from app.ai.openrouter import OpenRouterProvider
from app.ai.telemetry import AITelemetry
from app.config.settings import Settings
from app.core.errors import AIAuthenticationError, AIProviderError, RateLimitError
from app.domain.ai import AIRequest, AIResponse, UNSUPPORTED_CAPABILITIES

logger = logging.getLogger(__name__)

_UNAVAILABLE_MESSAGE = "عذراً، الخدمة غير متاحة حالياً. حاول لاحقاً."


class AIGateway:
    """Capability-routed async AI gateway with a provider failover chain."""

    def __init__(
        self,
        settings_: Settings,
        provider: Optional[OpenRouterProvider] = None,
        local: Optional[LocalProvider] = None,
        telemetry: Optional[AITelemetry] = None,
    ):
        self._settings = settings_
        self.provider = provider or OpenRouterProvider(settings_)
        self.local = local or LocalProvider()
        self.telemetry = telemetry or AITelemetry()

    def _fallback_advice(self, request: AIRequest) -> str:
        """Produce a capability-aware fallback when even local AI has nothing."""
        if request.capability in UNSUPPORTED_CAPABILITIES:
            return "⚠️ هذه الميزة غير متاحة حالياً."
        return _UNAVAILABLE_MESSAGE

    async def _try_provider(self, provider, request: AIRequest):
        """Run one provider with bounded retries.

        Returns (text, last_error, attempts_used). text is None on failure.
        Retries only transient errors (AIProviderError/timeout); rate-limit
        and authentication failures break out immediately - the next provider
        in the chain gets the request instead.
        """
        max_retries = max(0, self._settings.ai_max_retries)
        attempt = 0
        last_error: Optional[BaseException] = None
        stage_started = time.perf_counter()

        while attempt <= max_retries:
            if not provider.is_available:
                last_error = last_error or AIProviderError(
                    f"{getattr(provider, 'name', 'provider')} currently unavailable"
                )
                break

            try:
                text = await asyncio.wait_for(
                    provider.generate(request),
                    timeout=self._settings.ai_request_timeout,
                )
                return text, None, attempt
            except RateLimitError as exc:
                logger.warning(
                    f"Rate limit hit for {request.capability}: {exc}. "
                    "OpenRouter unavailable; using configured fallback"
                )
                return None, exc, attempt
            except AIAuthenticationError as exc:
                logger.error(
                    f"Authentication rejected by "
                    f"{getattr(provider, 'name', 'provider')}: {exc}; failing over"
                )
                return None, exc, attempt
            except (AIProviderError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                attempt += 1
                backoff = self._settings.ai_retry_backoff_base * (2 ** attempt)
                logger.info(
                    f"AI attempt {attempt}/{max_retries} failed on "
                    f"{getattr(provider, 'name', 'provider')} for "
                    f"'{request.capability}' "
                    f"({(time.perf_counter() - stage_started) * 1000:.0f}ms); "
                    f"retrying in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)

        return None, last_error, attempt

    async def generate(self, request: AIRequest) -> AIResponse:
        run_id = self.telemetry.next_run_id()
        started = time.perf_counter()
        category = request.category()

        if category in UNSUPPORTED_CAPABILITIES:
            return self._record(
                run_id, request, category, "none", "none", started, 0,
                self._fallback_advice(request), False, "unsupported capability",
            )

        # OpenRouter is the only cloud provider; local fallback is handled below.
        chain: List = [self.provider]
        last_error: Optional[BaseException] = None

        for provider in chain:
            text, error, attempts = await self._try_provider(provider, request)
            if text is not None:
                return self._record(
                    run_id, request, category,
                    getattr(provider, "name", "provider"),
                    getattr(provider, "model", "unknown"),
                    started, attempts, text, True, None,
                )
            if error is not None:
                last_error = error

        if self._settings.use_local_fallback:
            try:
                text = self.local.generate(request)
                return self._record(
                    run_id, request, category, self.local.name, "deterministic-local",
                    started, 0, text, True, None,
                )
            except Exception as exc:  # noqa: BLE001 - fallback must never crash the loop
                last_error = exc
                logger.error(f"Local fallback failed for {request.capability}: {exc}")

        error_text = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown"
        return self._record(
            run_id, request, category, "none", "none", started, 0,
            self._fallback_advice(request), False, error_text,
        )

    def _record(
        self,
        run_id: str,
        request: AIRequest,
        category: str,
        provider: str,
        model: str,
        started: float,
        retry_count: int,
        text: str,
        success: bool,
        error: Optional[str],
    ) -> AIResponse:
        latency_ms = (time.perf_counter() - started) * 1000
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
        """Arabic quota/status view (mirrors the original settings view)."""
        if not self.provider.is_available:
            if self._settings.use_local_fallback:
                return "⚠️ OpenRouter غير متاح - الردود المحلية مفعلة"
            return "⚠️ OpenRouter غير متاح - سيتم إرجاع الرد الآمن الجاهز"
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
