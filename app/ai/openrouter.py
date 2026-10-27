"""
AUSTRO AI - OpenRouter provider (secondary cloud provider).

Async provider for the OpenRouter chat-completions endpoint
(`POST {base}/chat/completions`). Network I/O runs on a worker thread
(asyncio.to_thread) using the same bounded threading pattern, so the Telegram update
loop is never blocked; calls are additionally bounded by `asyncio.wait_for`
in the gateway.

Authentication is verified once at startup via the cheap authenticated
`GET {base}/key` endpoint (no generation is spent on the health check).

Security: the API key appears only in the `Authorization` header. It is never
logged, never embedded in error messages, and any provider error text that
could echo the key back is redacted before it is raised.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import requests

from app.config.settings import Settings
from app.core.errors import (
    AIAuthenticationError,
    AIProviderError,
    RateLimitError,
)
from app.core.interfaces import AIProvider
from app.domain.ai import AIRequest

logger = logging.getLogger(__name__)

#: Conservative cap on requested completion tokens regardless of what the
#: caller asks for; free-tier models reject oversized asks and the gateway
#: rarely needs more than this for chat-scale answers.
_MAX_OUTPUT_TOKENS_CAP = 2048

#: HTTP statuses treated as transient for bounded gateway retries.
_TRANSIENT_STATUS = {408, 500, 502, 503, 504}


def _redact(text: str, key: str) -> str:
    """Strip the API key (and key-shaped fragments) from provider text."""
    if key:
        text = text.replace(key, "[REDACTED]")
    return text[:300]


class OpenRouterProvider(AIProvider):
    """Async OpenRouter provider with quota guard, pacing and redaction."""

    name = "openrouter"

    def __init__(self, settings_: Settings):
        self._settings = settings_
        self._available: bool = False
        self.request_count: int = 0
        self.last_request_time: float = 0.0
        # Guards the pacing decision and the quota counter (never held across
        # an await: the slot is reserved, then the caller sleeps outside).
        self._state_lock = threading.Lock()
        if not settings_.has_openrouter_key:
            logger.info(
                "OpenRouter API key not set; secondary provider is disabled"
            )
        else:
            self._available = self.health_check()

    # -- identity / availability -------------------------------------------- #
    @property
    def model(self) -> str:
        return self._settings.openrouter_model

    @property
    def is_available(self) -> bool:
        return self._available and self.request_count < self._settings.max_requests_per_day

    def health_check(self) -> bool:
        """Verify the key against GET /key (blocking; run in a thread).

        Confirms authentication without spending a generation request.
        """
        if not self._settings.has_openrouter_key:
            return False
        key = self._settings.openrouter_api_key
        url = f"{self._settings.openrouter_api_url}/key"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=self._settings.ai_healthcheck_timeout,
            )
        except requests.exceptions.Timeout:
            logger.warning("OpenRouter health check timed out; provider disabled")
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("OpenRouter unreachable; provider disabled")
            return False
        except Exception as exc:  # noqa: BLE001 - never crash startup
            logger.warning(
                f"OpenRouter health check failed: {type(exc).__name__}; provider disabled"
            )
            return False

        if response.status_code == 200:
            logger.info(
                f"OpenRouter connected (model={self.model})"
            )
            return True
        if response.status_code in (401, 403):
            logger.warning(
                "OpenRouter rejected the API key (401/403); provider disabled"
            )
            return False
        logger.warning(
            f"OpenRouter health check returned HTTP {response.status_code}; "
            "provider disabled"
        )
        return False

    # -- pacing / quota ------------------------------------------------------ #
    async def _rate_limit(self) -> None:
        """Reserve a send slot at least `min_request_interval` after the last."""
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
        with self._state_lock:
            self.request_count += 1

    # -- generation ---------------------------------------------------------- #
    def _build_payload(self, request: AIRequest) -> dict:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        return {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": min(request.max_tokens, _MAX_OUTPUT_TOKENS_CAP),
        }

    async def generate(self, request: AIRequest) -> str:
        """Call OpenRouter async; raises AUSTRO errors on failure."""
        if not self._available:
            raise AIProviderError("OpenRouter not available")
        if self.request_count >= self._settings.max_requests_per_day:
            self._available = False
            raise RateLimitError("Daily quota exceeded")

        await self._rate_limit()

        key = self._settings.openrouter_api_key
        url = f"{self._settings.openrouter_api_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (documented, optional).
            "HTTP-Referer": "https://github.com/Devmraustro/AUSTRO-AI",
            "X-Title": "AUSTRO AI",
        }
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
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AIProviderError("Malformed JSON from OpenRouter") from exc
                return self._extract_text(data)

            if response.status_code in (401, 403):
                # Credentials are wrong/revoked: fail over, do not retry.
                self._available = False
                raise AIAuthenticationError(
                    f"OpenRouter rejected credentials (HTTP {response.status_code})"
                )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                self._available = False
                detail = f"Retry-After={retry_after}" if retry_after else "no Retry-After"
                raise RateLimitError(
                    _redact(f"OpenRouter rate limited ({detail})", key)
                )

            if response.status_code == 400:
                message = self._error_message(response, key)
                raise AIProviderError(_redact(f"OpenRouter 400: {message}", key))

            if response.status_code in _TRANSIENT_STATUS:
                raise AIProviderError(
                    f"OpenRouter transient HTTP {response.status_code}"
                )

            raise AIProviderError(f"OpenRouter HTTP {response.status_code}")

        except requests.exceptions.Timeout as exc:
            # Transient: the provider stays available (preserves availability on transient errors).
            logger.warning(
                f"OpenRouter request timed out after {self._settings.ai_request_timeout}s"
            )
            raise AIProviderError("Request timeout") from exc
        except requests.exceptions.RequestException as exc:
            raise AIProviderError(
                _redact(f"OpenRouter request failed: {type(exc).__name__}", key)
            ) from exc

    def _extract_text(self, data: dict) -> str:
        """Pull non-empty assistant text out of a chat-completions response."""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIProviderError("OpenRouter returned no choices")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            raise AIProviderError("OpenRouter returned empty content")
        return text

    def _error_message(self, response, key: str) -> str:
        try:
            payload = response.json()
            message = payload.get("error", {})
            if isinstance(message, dict):
                message = message.get("message", "")
            return _redact(str(message), key)
        except Exception:  # noqa: BLE001 - diagnostics only
            return "no provider message"

    def reconnect(self) -> bool:
        """Re-run the health check and reset the quota counter on success."""
        self._available = self.health_check()
        if self._available:
            self.request_count = 0
        return self._available
