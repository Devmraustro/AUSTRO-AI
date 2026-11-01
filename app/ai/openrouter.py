"""
AUSTRO AI - OpenRouter provider (sole cloud provider).

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
import json
import logging
import threading
import math
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

#: Hard cap on the upstream response body that may be buffered, so a broken
#: or hostile endpoint cannot push unbounded bytes into memory.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024


def _redact(text: str, key: str) -> str:
    """Strip the API key (and key-shaped fragments) from provider text."""
    if key:
        text = text.replace(key, "[REDACTED]")
    return text[:300]


class OpenRouterProvider(AIProvider):
    """Async OpenRouter provider with quota guard, pacing and redaction.

    When free-only fallback models are configured, generation uses OpenRouter's
    native ordered `models` array (primary first) so the provider itself does
    the failover server-side; the app never sends one HTTP request per model.
    The actual model that answered is recorded per-request on the AIRequest.
    """

    name = "openrouter"

    def __init__(self, settings_: Settings):
        self._settings = settings_
        self._available: bool = False
        # Lifetime count of generation calls that reached a provider response
        # (stats only — never used for daily quota enforcement).
        self.request_count: int = 0
        self.last_request_time: float = 0.0
        # 429 cooldown: seconds-since-monotonic-epoch until which the provider
        # refuses sends. Bounded by AI_RATE_LIMIT_COOLDOWN_SECONDS and seeded
        # from a provider Retry-After when one is supplied.
        self._cooldown_until: float = 0.0
        # Daily free-quota state. This counter is the ONLY one quota
        # enforcement consults, and it rolls over at UTC midnight.
        # `_daily_used`/`_daily_limit` are seeded from the GET /key account
        # response when available (so an app restart does not reset the
        # provider-side quota); otherwise the counter is checked against
        # max_requests_per_day.
        self._daily_used: int = 0
        self._daily_limit: int = 0  # 0 = unknown -> fall back to settings cap
        self._quota_date: str = self._utc_today()
        self._daily_exhausted: bool = False
        # Guards quota reservation/checks and the pacing decision. Never held
        # across network I/O.
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
    def fallback_models(self) -> list:
        """Ordered free-only fallback ids (validated at settings load)."""
        return list(getattr(self._settings, "openrouter_fallback_models", ()) or ())

    @staticmethod
    def _utc_today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _maybe_reset_daily(self) -> None:
        """Roll the daily quota counter at UTC midnight (acquires the lock).

        Only usage resets: the account's daily limit is stable, so it is kept
        (and refreshed on the next /key health check) rather than forgotten.
        """
        with self._state_lock:
            self._reset_daily_if_needed_locked()

    def _reset_daily_if_needed_locked(self) -> None:
        """UTC-midnight rollover. Caller MUST hold `_state_lock`.

        Lock ownership contract for the daily-quota state (`_quota_date`,
        `_daily_used`, `_daily_limit`, `_daily_exhausted`): EVERY read and
        write happens under `_state_lock` — `_parse_key_quota` (seeding),
        `_reserve_daily_slot` (the sole authoritative admission decision),
        `is_available` / `daily_used` / `daily_limit` (reads), and this
        rollover. `generate()` touches no quota state before the
        reservation. No nested acquisition anywhere; the lock is never held
        across I/O.
        """
        today = self._utc_today()
        if today != self._quota_date:
            self._quota_date = today
            self._daily_exhausted = False
            self._daily_used = 0
            logger.info("UTC day rolled over; OpenRouter daily quota usage reset")

    def _effective_daily_limit(self) -> int:
        """Account quota from /key when known, else the configured ceiling."""
        if self._daily_limit > 0:
            return self._daily_limit
        return self._settings.max_requests_per_day

    @property
    def daily_used(self) -> int:
        """Requests consumed today (conservative: includes in-flight sends)."""
        with self._state_lock:
            self._reset_daily_if_needed_locked()
            return self._daily_used

    @property
    def daily_limit(self) -> int:
        """Today's effective daily quota (account value or configured cap)."""
        with self._state_lock:
            self._reset_daily_if_needed_locked()
            return self._effective_daily_limit()

    def _cooldown_active(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def cooldown_remaining(self) -> float:
        """Seconds left in the 429 cooldown (0 = no cooldown)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def is_available(self) -> bool:
        if not self._available:
            return False
        with self._state_lock:
            self._reset_daily_if_needed_locked()
            if self._cooldown_active():
                return False
            if self._daily_exhausted:
                return False
            return self._daily_used < self._effective_daily_limit()

    def _apply_rate_limit_headers(self, headers) -> None:
        """Start a bounded cooldown from Retry-After / rate-limit headers.

        Retry-After accepts delay-seconds or HTTP-date. The cooldown is always
        clamped to the configured ceiling so a hostile or extreme header can
        never disable the provider indefinitely.
        """
        ceiling = max(1.0, float(self._settings.ai_rate_limit_cooldown_seconds))
        delay: float = ceiling
        raw = ""
        try:
            raw = (headers.get("Retry-After") or "").strip() if headers else ""
        except Exception:  # noqa: BLE001 - headers are diagnostics only
            raw = ""
        if raw:
            if raw.isdigit():
                delay = float(raw)
            else:
                try:
                    from datetime import datetime as _datetime, timezone as _tz
                    from email.utils import parsedate_to_datetime
                    when = parsedate_to_datetime(raw)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=_tz.utc)
                    delay = max(0.0, (when - _datetime.now(_tz.utc)).total_seconds())
                except Exception:  # noqa: BLE001 - unparsable date -> ceiling
                    delay = ceiling
        delay = min(max(delay, 1.0), ceiling)
        self._cooldown_until = time.monotonic() + delay
        logger.info(
            f"OpenRouter cooldown started for {delay:.0f}s "
            f"(Retry-After={raw or 'not given'}; "
            f"x-ratelimit-limit={headers.get('X-RateLimit-Limit', '') if headers else ''}, "
            f"remaining={headers.get('X-RateLimit-Remaining', '') if headers else ''}, "
            f"reset={headers.get('X-RateLimit-Reset', '') if headers else ''})"
        )

    @staticmethod
    def _quota_number(value) -> bool:
        """Accept only values representing valid non-negative integer counts.

        Integers are accepted (booleans excluded); floats only when finite
        AND integral (5.0 ok, 5.9, NaN and both infinities rejected) so the
        later int() conversion can never raise or silently truncate.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float):
            return math.isfinite(value) and value == int(value)
        return True

    def _parse_key_quota(self, payload: dict) -> None:
        """Apply a GET /key quota snapshot, atomically and all-or-nothing.

        The COMPLETE snapshot (usage AND limit) is validated before any
        state changes: a snapshot missing or malformed on either side, with
        negative usage or a non-positive limit, or carrying booleans, is
        rejected wholesale — the previous valid state survives untouched.
        Legacy representations (a numeric block as the limit plus a
        separate usage field) are honoured only when both parts validate.
        The applied snapshot synchronizes `_quota_date` with the current
        UTC date in the same critical section, so a reconnect after a UTC
        rollover cannot have its seeding wiped by a stale-date rollover
        check; `_daily_exhausted` is recomputed from the validated pair.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        block = data.get("free_model_daily_requests")
        used = limit = None
        if isinstance(block, dict):
            used = block.get("used")
            limit = block.get("limit")
        elif self._quota_number(block):
            limit = block
            used = data.get("free_model_daily_usage")
        # All-or-nothing: both fields must validate, or nothing is applied.
        if not (self._quota_number(used) and self._quota_number(limit)):
            return
        used_i, limit_i = int(used), int(limit)
        if used_i < 0 or limit_i <= 0:
            return
        with self._state_lock:
            self._quota_date = self._utc_today()
            self._daily_used = used_i
            self._daily_limit = limit_i
            self._daily_exhausted = used_i >= limit_i
            if self._daily_exhausted:
                logger.warning(
                    "OpenRouter daily free quota exhausted "
                    f"({used_i}/{limit_i}); local fallback"
                )
            else:
                logger.info(
                    f"OpenRouter daily free quota: {used_i}/{limit_i} used"
                )

    def health_check(self) -> bool:
        """Verify the key against GET /key (blocking; run in a thread).

        Confirms authentication without spending a generation request, and
        seeds the daily free-quota state from the account response.
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
            try:
                self._parse_key_quota(response.json())
            except ValueError:
                logger.info("OpenRouter /key returned a non-JSON body; quota state unknown")
            chain = [self.model] + self.fallback_models
            logger.info(
                f"OpenRouter connected (model chain={' -> '.join(chain)})"
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
        """Lifetime stats: a generation that reached a provider response."""
        with self._state_lock:
            self.request_count += 1

    def _reserve_daily_slot(self) -> None:
        """Atomically reserve one daily-quota slot before any network I/O.

        Reservation happens under `_state_lock` so concurrent tasks cannot all
        pass the check before any of them increments the counter. The slot is
        consumed conservatively: if the HTTP request later fails or times out
        and it is uncertain whether OpenRouter received (and billed) it, the
        reservation stays — under-counting quota is worse than a slightly
        early cooldown. The lock is never held across the network call.
        """
        with self._state_lock:
            self._reset_daily_if_needed_locked()
            if not self._daily_exhausted and self._daily_used >= self._effective_daily_limit():
                self._daily_exhausted = True
                logger.warning(
                    "OpenRouter daily quota exhausted "
                    f"({self._daily_used}/{self._effective_daily_limit()}); "
                    "routing to local fallback until the UTC reset"
                )
            if self._daily_exhausted:
                raise RateLimitError("Daily quota exceeded")
            self._daily_used += 1
            if self._daily_used >= self._effective_daily_limit():
                # Eagerly flag: this send was the day's last allowed slot.
                self._daily_exhausted = True

    # -- generation ---------------------------------------------------------- #
    def _build_payload(self, request: AIRequest) -> dict:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        payload = {
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": min(request.max_tokens, _MAX_OUTPUT_TOKENS_CAP),
        }
        fallbacks = self.fallback_models
        if fallbacks:
            # Native OpenRouter routing: an ordered `models` array (primary
            # first) makes OpenRouter itself fail over server-side. `model`
            # and `models` are never sent together.
            payload["models"] = [self.model, *fallbacks]
        else:
            payload["model"] = self.model
        return payload

    async def generate(self, request: AIRequest) -> str:
        """Call OpenRouter async; raises AUSTRO errors on failure.

        Quota admission is decided EXCLUSIVELY by `_reserve_daily_slot()`,
        the last lock-protected step before the HTTP call: an advisory check
        here could reject a request on state that a concurrent `/key`
        snapshot or UTC rollover has already restored (stale rejection), or
        admit one it has already exhausted. No quota state is read or
        written before the reservation."""
        if not self._available:
            raise AIProviderError("OpenRouter not available")
        if self._cooldown_active():
            raise RateLimitError(
                f"OpenRouter cooling down for another "
                f"{self.cooldown_remaining():.0f}s after rate limiting"
            )

        # Pacing happens BEFORE the quota reservation: a task cancelled while
        # waiting to send must not burn a daily slot it never used.
        await self._rate_limit()

        # Atomic reservation: the FINAL quota admission decision, executed as
        # the last step before the HTTP call (no await in between, so
        # concurrent tasks can never exceed the remaining slots). It re-rolls
        # the UTC date and re-checks exhaustion under the lock, honoring any
        # concurrent /key snapshot or rollover - never a stale rejection. The
        # slot is held even if the request later fails - conservative
        # accounting when the provider may have billed it.
        self._reserve_daily_slot()

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
                # Streaming keeps the body read behind the same bounded-read
                # cap as the parse step below.
                stream=True,
            )
            self._count_request()

            try:
                if response.status_code == 200:
                    data = await asyncio.to_thread(self._bounded_payload, response)
                    text = self._extract_text(data)
                    # Attribute the run to the model that ACTUALLY answered
                    # (OpenRouter reports it on the response), scoped to this
                    # request object so concurrent runs never cross.
                    actual = data.get("model") if isinstance(data, dict) else None
                    if isinstance(actual, str) and actual.strip():
                        request.data["_actual_model"] = actual.strip()
                    return text

                if response.status_code in (401, 403):
                    # Credentials are wrong/revoked: fail over, do not retry.
                    self._available = False
                    raise AIAuthenticationError(
                        f"OpenRouter rejected credentials (HTTP {response.status_code})"
                    )

                if response.status_code == 429:
                    # NOT a permanent outage: start a bounded cooldown seeded
                    # from Retry-After / rate-limit headers, then recover
                    # automatically.
                    headers_map = getattr(response, "headers", {}) or {}
                    self._apply_rate_limit_headers(headers_map)
                    retry_after = headers_map.get("Retry-After", "")
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
            finally:
                # The response is closed exactly once on EVERY path once it
                # has been received: success after read/extract, JSON parse
                # failure, stream read failure, size-cap refusal, 400,
                # 401/403, 429, 5xx and any other exception.
                response.close()

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

    def _extract_text(self, data) -> str:
        """Pull non-empty assistant text out of a chat-completions response.

        The response schema is validated explicitly at every level (JSON
        object, non-empty `choices` list, dict choice, dict message, string
        non-empty `content`) so a malformed upstream body becomes a safe
        `AIProviderError` — never an `AttributeError` or `TypeError` — and
        no raw response content reaches the error messages.
        """
        if not isinstance(data, dict):
            raise AIProviderError(
                "OpenRouter returned a malformed response (not a JSON object)"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIProviderError("OpenRouter returned no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise AIProviderError("OpenRouter returned a malformed choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise AIProviderError("OpenRouter returned a malformed message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AIProviderError("OpenRouter returned empty content")
        return content

    def _bounded_bytes(self, response) -> bytes:
        """Read a response body with a hard byte cap.

        A declared Content-Length beyond the cap fails before reading; the
        streamed read fails as soon as the cap is exceeded, so a hostile or
        broken endpoint can never buffer unbounded bytes. Runs on a worker
        thread (the callers use asyncio.to_thread), keeping the event loop
        untouched.
        """
        declared = ""
        try:
            declared = (response.headers.get("Content-Length") or "").strip()
            if declared and int(declared) > _MAX_RESPONSE_BYTES:
                raise AIProviderError("OpenRouter response body exceeds the size limit")
        except ValueError:
            declared = ""
        chunks = []
        received = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise AIProviderError(
                            "OpenRouter response body exceeds the size limit"
                        )
                    chunks.append(chunk)
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            raise AIProviderError(_redact(
                f"OpenRouter body read failed: {type(exc).__name__}",
                self._settings.openrouter_api_key,
            )) from exc
        return b"".join(chunks)

    def _bounded_payload(self, response) -> dict:
        """Parse the success body read under the hard byte cap."""
        try:
            return json.loads(self._bounded_bytes(response).decode("utf-8"))
        except AIProviderError:
            raise
        except (ValueError, UnicodeDecodeError) as exc:
            raise AIProviderError("Malformed JSON from OpenRouter") from exc

    def _error_message(self, response, key: str) -> str:
        """Read a (small) provider error body under the same byte cap."""
        try:
            payload = json.loads(self._bounded_bytes(response).decode("utf-8"))
            message = payload.get("error", {})
            if isinstance(message, dict):
                message = message.get("message", "")
            return _redact(str(message), key)
        except Exception:  # noqa: BLE001 - diagnostics only
            return "no provider message"

    def reconnect(self) -> bool:
        """Re-run the health check; clears any cooldown and re-reads quota.

        The quota counter is re-seeded from the /key account response rather
        than zeroed, so a manual reconnect cannot conjure fresh usage.
        """
        self._available = self.health_check()
        self._cooldown_until = 0.0
        return self._available
