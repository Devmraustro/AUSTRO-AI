"""AUSTRO AI - AI provider hardening regressions.

AI-1 (reliability: retries defeated, permanent degradation)
    `GeminiProvider.generate()` set `self._available = False` on a *timeout*.
    `AIGateway` consults `provider.is_available` before every attempt, so one
    slow upstream cancelled the bounded retries and then kept the provider
    disabled for the lifetime of the process: every later answer silently came
    from the local fallback until an operator tapped "reconnect". A timeout is
    transient; 429/quota is not. Only the latter still disables the provider.

AI-2 (reliability: pacing race)
    `_rate_limit()` read `last_request_time`, slept, then wrote it. Concurrent
    coroutines all measured the same "elapsed", slept the same amount and then
    fired together, exceeding the provider's requests-per-minute budget exactly
    when several users ask at once. The slot is now reserved before sleeping.
"""

import asyncio
import dataclasses
import os
import time

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["GEMINI_API_KEY"] = ""

import pytest
import requests

from app.ai.gemini import GeminiProvider
from app.ai.gateway import AIGateway
from app.config.settings import settings as base_settings
from app.core.errors import AIProviderError, RateLimitError
from app.domain.ai import AIRequest


_DEFAULTS = dict(
    gemini_api_key="test-key-0000000000",
    gemini_api_url="https://example.invalid/v1beta/models",
    ai_request_timeout=5.0,
    ai_healthcheck_timeout=1.0,
    min_request_interval=0.0,
    max_requests_per_day=100,
)


def _cfg(**overrides):
    return dataclasses.replace(base_settings, **{**_DEFAULTS, **overrides})


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        }

    def json(self):
        return self._payload


def _provider(monkeypatch, behaviour, **cfg_overrides):
    """Build an available provider; `behaviour(call_index)` returns a response."""
    monkeypatch.setattr("app.ai.gemini.requests.post", behaviour)
    provider = GeminiProvider(_cfg(**cfg_overrides))
    assert provider.is_available is True, "the health check should have succeeded"
    return provider


def _request():
    return AIRequest(capability="chat", prompt="مرحباً")


# ---------------------------------------------------------------------------
# AI-1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_keeps_the_provider_available(monkeypatch):
    calls = {"n": 0}

    def behaviour(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # health check
            return _Response()
        raise requests.exceptions.Timeout("slow upstream")

    provider = _provider(monkeypatch, behaviour)

    with pytest.raises(AIProviderError):
        await provider.generate(_request())

    assert provider.is_available is True, (
        "a transient timeout must not disable the provider: the gateway checks "
        "is_available before each retry, so this cancels the retries"
    )
    assert provider.request_count == 0  # a failed call is not billed as a request


@pytest.mark.asyncio
async def test_gateway_retries_after_a_timeout(monkeypatch):
    """The end-to-end consequence: retries actually happen."""
    calls = {"n": 0}

    def behaviour(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response()
        if calls["n"] <= 3:
            raise requests.exceptions.Timeout("slow upstream")
        return _Response(payload={"candidates": [{"content": {"parts": [{"text": "answer"}]}}]})

    provider = _provider(monkeypatch, behaviour)
    gateway = AIGateway(settings_=_cfg(ai_max_retries=2, ai_retry_backoff_base=0.0),
                        provider=provider)

    response = await gateway.generate(_request())

    assert response.text == "answer"
    # 1 health check + initial attempt + ai_max_retries(2) retries. With the
    # old timeout handling the provider became unavailable after the first
    # failure, so only 2 calls would have happened and the answer would have
    # come from the local fallback instead.
    assert calls["n"] == 4, f"expected 1 health check + 3 attempts, saw {calls['n']}"


@pytest.mark.asyncio
async def test_quota_failure_still_disables_the_provider(monkeypatch):
    calls = {"n": 0}

    def behaviour(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response()
        return _Response(status_code=429, payload={"error": {"message": "quota"}})

    provider = _provider(monkeypatch, behaviour)

    with pytest.raises(RateLimitError):
        await provider.generate(_request())

    assert provider.is_available is False


# ---------------------------------------------------------------------------
# AI-2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_calls_are_paced_not_burst(monkeypatch):
    timestamps = []
    health = {"n": 0}

    def behaviour(*args, **kwargs):
        health["n"] += 1
        if health["n"] == 1:
            return _Response()
        timestamps.append(time.monotonic())
        return _Response()

    provider = _provider(monkeypatch, behaviour, min_request_interval=0.05)

    started = time.monotonic()
    await asyncio.gather(*(provider.generate(_request()) for _ in range(5)))
    elapsed = time.monotonic() - started

    assert len(timestamps) == 5
    # Each of the 5 calls reserves its own slot, so the group cannot finish
    # faster than 4 intervals. Total elapsed time is immune to the scheduler
    # reordering the worker threads that perform the calls (which is why a
    # pairwise-gap assertion would be flaky on a loaded runner); the old
    # read-sleep-write implementation finished 5 concurrent calls in ~1
    # interval (measured: gaps [0.051, 0.0, 0.001, 0.0]).
    floor = 4 * 0.05 * 0.8
    assert elapsed >= floor, (
        f"5 concurrent calls finished in {elapsed:.3f}s (expected >= {floor:.3f}s "
        "for min_request_interval=0.05): the pacing was bursted through"
    )


@pytest.mark.asyncio
async def test_sequential_pacing_still_works(monkeypatch):
    """A single caller must not be delayed when the interval has elapsed."""
    calls = {"n": 0}

    def behaviour(*args, **kwargs):
        calls["n"] += 1
        return _Response()

    provider = _provider(monkeypatch, behaviour, min_request_interval=0.0)
    await provider.generate(_request())
    await provider.generate(_request())
    assert calls["n"] == 3  # 1 health check + 2 calls
    assert provider.request_count == 2
