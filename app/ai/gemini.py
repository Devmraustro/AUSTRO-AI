"""
AUSTRO AI - Gemini provider.

Async provider for the Gemini v1beta generateContent endpoint. Network I/O
runs on a worker thread (asyncio.to_thread) so the update loop is never
blocked; calls are additionally bounded by `asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

import requests

from app.config.settings import Settings
from app.core.errors import AIProviderError, RateLimitError
from app.core.interfaces import AIProvider
from app.domain.ai import AIRequest

logger = logging.getLogger(__name__)


def fallback_status(settings_: Settings) -> str:
    """One-line statement of the configured fallback state for logs.

    Reports whether local fallback is enabled/available without claiming it has
    already executed.
    """
    if settings_.use_local_fallback:
        return "Local AI fallback is enabled."
    return (
        "Local AI fallback is disabled; the gateway will return its safe "
        "built-in response if Gemini is unavailable."
    )


_GEMINI_VERSION_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


def supports_sampling_params(model: str) -> bool:
    """Whether the model honors custom temperature/topP/topK.

    Google recommends default sampling settings across Gemini 3 models;
    Gemini 3.6+ and Gemini 3.5 Flash-Lite additionally deprecate explicit
    values and may reject them in future API generations. We therefore omit
    custom sampling fields for all Gemini 3 models. Gemini 2.5 and earlier
    supported models retain the historical payload. Unknown identifiers keep
    the old behaviour, so a mis-typed model never silently changes the request.
    """
    match = _GEMINI_VERSION_RE.match(model or "")
    if not match:
        return True
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    if major == 3:
        # Gemini 3 models are optimized for default sampling values; later
        # families deprecate explicit values and may reject them entirely.
        return False
    return major < 3


def _generation_config(model: str, max_tokens: int, temperature: float) -> dict:
    """Build `generationConfig`, omitting sampling knobs newer models reject."""
    config = {"maxOutputTokens": max_tokens}
    if supports_sampling_params(model):
        config.update({"temperature": temperature, "topP": 0.9, "topK": 40})
    return config


_HARM_CATEGORIES = [
    ("HARM_CATEGORY_HARASSMENT", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_HATE_SPEECH", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_SEXUALLY_EXPLICIT", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_DANGEROUS_CONTENT", "BLOCK_MEDIUM_AND_ABOVE"),
]


class GeminiProvider(AIProvider):
    """Async Gemini flash provider with quota guard and rate limiting."""

    name = "gemini"

    def __init__(self, settings_: Settings):
        self._settings = settings_
        self._available: bool = False
        self.request_count: int = 0
        self.last_request_time: float = 0.0
        # Guards the pacing decision and the quota counter. Never held across an
        # await: the slot is reserved, then the caller sleeps outside the lock.
        self._state_lock = threading.Lock()
        if not settings_.has_gemini_key:
            logger.warning(f"Gemini API key not set. {fallback_status(settings_)}")
        else:
            self._available = self.health_check()

    @property
    def model(self) -> str:
        return self._settings.gemini_model

    @property
    def is_available(self) -> bool:
        return self._available and self.request_count < self._settings.max_requests_per_day

    def health_check(self) -> bool:
        """Check whether Gemini is reachable (blocking; run in a thread)."""
        if not self._settings.has_gemini_key:
            return False
        try:
            url = f"{self._settings.gemini_api_url}/{self.model}:generateContent"
            headers = {"x-goog-api-key": self._settings.gemini_api_key}
            payload = {
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "generationConfig": _generation_config(
                    self.model, 10, temperature=0.7
                ),
            }
            response = requests.post(
                url, headers=headers, json=payload,
                timeout=self._settings.ai_healthcheck_timeout,
            )
            if response.status_code == 200:
                data = response.json()
                if "candidates" in data and data["candidates"]:
                    logger.info(
                        f"Gemini connected (model={self.model}; "
                        f"{self._settings.max_requests_per_day} requests/day budget)"
                    )
                    return True
            error_msg = "Unknown error"
            try:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get(
                    "message", f"Status {response.status_code}"
                )
            except Exception:  # noqa: BLE001 - non-blocking diagnostic
                error_msg = f"Status {response.status_code}"
            logger.warning(
                f"Gemini API error: {error_msg}. {fallback_status(self._settings)}"
            )
            return False
        except requests.exceptions.Timeout:
            logger.warning(
                f"Gemini connection timed out. {fallback_status(self._settings)}"
            )
            return False
        except requests.exceptions.ConnectionError:
            logger.warning(
                f"No internet connection. {fallback_status(self._settings)}"
            )
            return False
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.warning(
                f"Gemini connection failed: {type(exc).__name__}. "
                f"{fallback_status(self._settings)}"
            )
            return False

    async def _rate_limit(self) -> None:
        """Reserve a send slot at least `min_request_interval` after the last one.

        The previous implementation read `last_request_time`, slept, and then
        wrote it back. Two coroutines that observed the same last send both slept
        the same amount and then fired together, so concurrent users could exceed
        the provider's requests-per-minute limit - precisely the situation the
        pacing exists for. The slot is now reserved atomically *before* sleeping,
        so waiters queue behind each other instead of bursting.
        """
        interval = self._settings.min_request_interval
        if interval <= 0:
            with self._state_lock:
                self.last_request_time = time.monotonic()
            return
        while True:
            with self._state_lock:
                now = time.monotonic()
                elapsed = now - self.last_request_time
                if elapsed >= interval:
                    self.last_request_time = now
                    return
                delay = interval - elapsed
            await asyncio.sleep(delay)

    def _count_request(self) -> None:
        """Account for one provider call (thread-safe quota counter)."""
        with self._state_lock:
            self.request_count += 1

    def _build_payload(self, request: AIRequest) -> dict:
        contents = []
        if request.system_prompt:
            contents.append({"role": "user", "parts": [{"text": request.system_prompt}]})
        contents.append({"role": "user", "parts": [{"text": request.prompt}]})
        return {
            "contents": contents,
            "generationConfig": _generation_config(
                self.model, request.max_tokens, temperature=request.temperature
            ),
            "safetySettings": [
                {"category": category, "threshold": threshold}
                for category, threshold in _HARM_CATEGORIES
            ],
        }

    async def generate(self, request: AIRequest) -> str:
        """Call Gemini async; raises AUSTRO errors on failure."""
        if not self._available:
            raise AIProviderError("Gemini not available")
        if self.request_count >= self._settings.max_requests_per_day:
            self._available = False
            raise RateLimitError("Daily quota exceeded")

        await self._rate_limit()

        url = f"{self._settings.gemini_api_url}/{self.model}:generateContent"
        headers = {"x-goog-api-key": self._settings.gemini_api_key}
        payload = self._build_payload(request)

        try:
            response = await asyncio.to_thread(
                requests.post,
                url,
                headers=headers,
                json=payload,
                timeout=self._settings.ai_request_timeout,
            )
            self._count_request()

            if response.status_code == 200:
                data = response.json()
                if "promptFeedback" in data and data["promptFeedback"].get("blockReason"):
                    return "⚠️ تم حظر الرد لأسباب أمان. حاول بصياغة مختلفة."
                if "candidates" in data and data["candidates"]:
                    candidate = data["candidates"][0]
                    finish_reason = candidate.get("finishReason", "")
                    if finish_reason == "SAFETY":
                        return "⚠️ الرد تم حظره لأسباب أمان."
                    if finish_reason == "MAX_TOKENS":
                        logger.warning("Response truncated due to max tokens")
                    content = candidate.get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        text_parts = [part.get("text", "") for part in parts if "text" in part]
                        return " ".join(text_parts).strip()
                return "عذراً، لم أتمكن من توليد رد."

            if response.status_code == 429:
                self._available = False
                raise RateLimitError("Quota exceeded")

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                raise AIProviderError(f"API Error 400: {error_msg}")

            raise AIProviderError(f"API Error: {response.status_code}")

        except requests.exceptions.Timeout as exc:
            # A timeout is transient, so the provider stays "available". The
            # gateway checks `is_available` before every retry; flipping it here
            # silently cancelled the bounded retries and left the bot answering
            # from local fallback until someone tapped "reconnect" in settings.
            logger.warning(
                f"Gemini request timed out after {self._settings.ai_request_timeout}s"
            )
            raise AIProviderError("Request timeout") from exc
        except requests.exceptions.RequestException as exc:
            raise AIProviderError(f"Request failed: {exc}") from exc

    def reconnect(self) -> bool:
        """Re-run the health check and reset the quota counter on success."""
        self._available = self.health_check()
        if self._available:
            self.request_count = 0
        return self._available