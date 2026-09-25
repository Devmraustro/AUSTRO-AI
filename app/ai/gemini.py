"""
AUSTRO AI - Gemini provider.

Async provider for the Gemini v1beta generateContent endpoint. Network I/O
runs on a worker thread (asyncio.to_thread) so the update loop is never
blocked; calls are additionally bounded by `asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
import time

import requests

from app.config.settings import Settings
from app.core.errors import AIProviderError, RateLimitError
from app.core.interfaces import AIProvider
from app.domain.ai import AIRequest

logger = logging.getLogger(__name__)

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
        if not settings_.has_gemini_key:
            logger.warning("Gemini API key not set. Using local AI fallback.")
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
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 10},
            }
            response = requests.post(
                url, headers=headers, json=payload,
                timeout=self._settings.ai_healthcheck_timeout,
            )
            if response.status_code == 200:
                data = response.json()
                if "candidates" in data and data["candidates"]:
                    logger.info("Gemini Flash 2.5 connected! (FREE - 1500 requests/day)")
                    return True
            error_msg = "Unknown error"
            try:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get(
                    "message", f"Status {response.status_code}"
                )
            except Exception:  # noqa: BLE001 - non-blocking diagnostic
                error_msg = f"Status {response.status_code}"
            logger.warning(f"Gemini API error: {error_msg}. Using local fallback.")
            return False
        except requests.exceptions.Timeout:
            logger.warning("Gemini connection timed out. Using local fallback.")
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("No internet connection. Using local fallback.")
            return False
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.warning(f"Gemini connection failed: {exc}. Using local fallback.")
            return False

    async def _rate_limit(self) -> None:
        """Async rate limiting to avoid hitting API limits."""
        elapsed = time.monotonic() - self.last_request_time
        if elapsed < self._settings.min_request_interval:
            await asyncio.sleep(self._settings.min_request_interval - elapsed)
        self.last_request_time = time.monotonic()

    def _build_payload(self, request: AIRequest) -> dict:
        contents = []
        if request.system_prompt:
            contents.append({"role": "user", "parts": [{"text": request.system_prompt}]})
        contents.append({"role": "user", "parts": [{"text": request.prompt}]})
        return {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
                "topP": 0.9,
                "topK": 40,
            },
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
            self.request_count += 1

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
                raise RateLimitError("Quota exceeded - switching to local AI")

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                raise AIProviderError(f"API Error 400: {error_msg}")

            raise AIProviderError(f"API Error: {response.status_code}")

        except requests.exceptions.Timeout as exc:
            self._available = False
            raise AIProviderError("Request timeout") from exc
        except requests.exceptions.RequestException as exc:
            raise AIProviderError(f"Request failed: {exc}") from exc

    def reconnect(self) -> bool:
        """Re-run the health check and reset the quota counter on success."""
        self._available = self.health_check()
        if self._available:
            self.request_count = 0
        return self._available