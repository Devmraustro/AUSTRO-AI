"""Free-only multi-model router tests.

Covers: native `models` array payload, free-only validation, actual-model
attribution (concurrency-safe), 429 cooldown with Retry-After / rate-limit
headers, daily quota from GET /key with UTC midnight reset and restart
seeding, safe public error messages, and gateway fallback to LocalProvider.
All HTTP is mocked; no live calls.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ.setdefault("OPENROUTER_API_KEY", "")

import pytest  # noqa: E402
import requests  # noqa: E402

from app.ai.gateway import AIGateway  # noqa: E402
from app.ai.local import LocalProvider  # noqa: E402
from app.ai.openrouter import OpenRouterProvider  # noqa: E402
from app.ai.telemetry import AITelemetry  # noqa: E402
from app.config.settings import (  # noqa: E402
    Settings,
    _parse_free_fallback_models,
    settings as base_settings,
)
from app.core.errors import (  # noqa: E402
    AIProviderError,
    ConfigurationError,
    RateLimitError,
    ValidationError,
    public_message,
)
from app.core.interfaces import AIProvider  # noqa: E402
from app.domain.ai import AIRequest  # noqa: E402

_OR_KEY = "sk-or-test-key-0000000000"

_DEFAULTS = dict(
    openrouter_api_key=_OR_KEY,
    openrouter_model="google/gemma-4-31b-it:free",
    openrouter_api_url="https://openrouter.example/api/v1",
    ai_request_timeout=5.0,
    ai_healthcheck_timeout=5.0,
    min_request_interval=0.0,
    max_requests_per_day=100,
    ai_max_retries=1,
    ai_retry_backoff_base=0.0,
    ai_rate_limit_cooldown_seconds=120.0,
    openrouter_fallback_models=(),
    openrouter_max_fallback_models=2,
    use_local_fallback=True,
)


def _cfg(**overrides) -> Settings:
    return dataclasses.replace(base_settings, **{**_DEFAULTS, **overrides})


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None, raw=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "ok"}}]
        }
        if raw is not None:
            self._raw = raw
        else:
            self._raw = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        for i in range(0, len(self._raw), max(1, chunk_size or 65536)):
            yield self._raw[i:i + (chunk_size or 65536)]


def _ok_payload(text="hello", model=None):
    payload = {"choices": [{"message": {"content": text}}]}
    if model:
        payload["model"] = model
    return payload


def _provider(monkeypatch, behaviour, key_response=None, **cfg):
    """Provider whose HTTP is mocked.

    `behaviour(url, **kwargs)` handles the POST completions call. GET /key
    (the health check) returns `key_response` when given, else a healthy
    200 with no quota data, so scenario responses apply to generation only
    unless a test explicitly configures the key endpoint.
    """
    def routed(url, **kwargs):
        if "/key" in url:
            return key_response if key_response is not None else _Response(payload={"data": {}})
        return behaviour(url, **kwargs)

    monkeypatch.setattr("app.ai.openrouter.requests.get", routed)
    monkeypatch.setattr("app.ai.openrouter.requests.post", routed)
    return OpenRouterProvider(_cfg(**cfg))


def _request(**kw):
    return AIRequest(capability="chat", prompt="hi", **kw)


def _arrange_quota_state(provider, *, date=None, used=None, limit=None,
                         exhausted=None, cooldown_until=None):
    """Test arrangement of quota state must respect the provider's lock
    contract: every mutation of the daily-quota fields happens under
    `_state_lock`, even in single-threaded setup."""
    with provider._state_lock:
        if date is not None:
            provider._quota_date = date
        if used is not None:
            provider._daily_used = used
        if limit is not None:
            provider._daily_limit = limit
        if exhausted is not None:
            provider._daily_exhausted = exhausted
        if cooldown_until is not None:
            provider._cooldown_until = cooldown_until


# --------------------------------------------------------------------------- #
# Phase 2: free-only configuration                                            #
# --------------------------------------------------------------------------- #
def test_fallback_models_parse_dedupes_and_strips():
    assert _parse_free_fallback_models(
        " vendor/a:free , vendor/a:free , vendor/b:free ,  , vendor/c:free ", 10
    ) == ("vendor/a:free", "vendor/b:free", "vendor/c:free")


def test_fallback_models_cap_is_enforced():
    assert _parse_free_fallback_models("vendor/a:free,vendor/b:free,vendor/c:free", 2) == (
        "vendor/a:free", "vendor/b:free",
    )
    assert _parse_free_fallback_models("vendor/a:free,vendor/b:free", 0) == ()


def test_paid_model_is_rejected_at_startup():
    with pytest.raises(ConfigurationError, match="free models only"):
        _parse_free_fallback_models("openai/gpt-5-mini", 2)


def test_dynamic_free_router_identifier_is_accepted():
    assert _parse_free_fallback_models("openrouter/free", 2) == ("openrouter/free",)


def test_paid_model_rejected_through_settings_loader(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODELS", "anthropic/claude-sonnet")
    with pytest.raises(ConfigurationError, match="free models only"):
        from app.config.settings import load_settings
        load_settings()


# --------------------------------------------------------------------------- #
# Phase 3: native `models` array payload                                      #
# --------------------------------------------------------------------------- #
async def test_payload_uses_models_array_when_fallbacks_configured(monkeypatch):
    captured = {}

    def behaviour(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour,
        openrouter_fallback_models=("nvidia/nemotron-3.5-lightning:free",),
    )
    await provider.generate(_request())
    assert captured["models"] == [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    assert "model" not in captured, "model and models must never be sent together"


async def test_payload_uses_single_model_without_fallbacks(monkeypatch):
    captured = {}

    def behaviour(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _Response(payload=_ok_payload("ok"))

    provider = _provider(monkeypatch, behaviour)
    await provider.generate(_request())
    assert captured["model"] == "google/gemma-4-31b-it:free"
    assert "models" not in captured


# --------------------------------------------------------------------------- #
# Phase 4: actual-model attribution                                           #
# --------------------------------------------------------------------------- #
async def test_actual_model_recorded_per_request(monkeypatch):
    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="openrouter/free"))

    provider = _provider(monkeypatch, behaviour)
    request = _request()
    await provider.generate(request)
    assert request.data["_actual_model"] == "openrouter/free"


async def test_gateway_telemetry_uses_actual_model(monkeypatch):
    class _ReportingProvider(AIProvider):
        name = "openrouter"

        @property
        def is_available(self):
            return True

        @property
        def model(self):
            return "google/gemma-4-31b-it:free"

        async def generate(self, request):
            request.data["_actual_model"] = "nvidia/nemotron-3.5-lightning:free"
            return "answer"

    gateway = AIGateway(
        _cfg(), provider=_ReportingProvider(), local=LocalProvider(),
        telemetry=AITelemetry(),
    )
    response = await gateway.generate(_request(user_id=1))
    assert response.metadata.model == "nvidia/nemotron-3.5-lightning:free"
    assert response.metadata.provider == "openrouter"


async def test_gateway_attribution_is_concurrency_safe():
    """Two concurrent runs reporting different actual models never cross."""
    class _RacingProvider(AIProvider):
        name = "openrouter"

        @property
        def is_available(self):
            return True

        @property
        def model(self):
            return "google/gemma-4-31b-it:free"

        async def generate(self, request):
            user = request.user_id
            await asyncio.sleep(0.02 if user == 1 else 0.01)
            actual = f"model-for-user-{user}"
            request.data["_actual_model"] = actual
            return f"reply-{user}"

    gateway = AIGateway(
        _cfg(), provider=_RacingProvider(), local=LocalProvider(),
        telemetry=AITelemetry(),
    )
    responses = await asyncio.gather(
        gateway.generate(_request(user_id=1)),
        gateway.generate(_request(user_id=2)),
    )
    assert responses[0].metadata.model == "model-for-user-1"
    assert responses[1].metadata.model == "model-for-user-2"
    # each metadata pairs with its own response text, not the other run's
    assert responses[0].text == "reply-1" and responses[1].text == "reply-2"


# --------------------------------------------------------------------------- #
# Phase 5: 429 cooldown, Retry-After, daily quota                             #
# --------------------------------------------------------------------------- #
async def test_429_starts_bounded_cooldown_not_permanent_disable(monkeypatch):
    calls = {"n": 0}

    def behaviour(url, **kwargs):
        calls["n"] += 1
        return _Response(status_code=429, headers={"Retry-After": "7"})

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(RateLimitError):
        await provider.generate(_request())
    assert provider.is_available is False, "cooldown active right after 429"
    remaining = provider.cooldown_remaining()
    assert 0 < remaining <= 7.5, "Retry-After=7 honored inside the ceiling"
    assert calls["n"] == 1
    # cooldown expiry restores availability without a reconnect
    _arrange_quota_state(provider, cooldown_until=time.monotonic() - 0.01)
    assert provider.is_available is True


async def test_retry_after_http_date_is_honored(monkeypatch):
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta

    when = datetime.now(timezone.utc) + timedelta(seconds=5)
    def behaviour(url, **kwargs):
        return _Response(status_code=429, headers={
            "Retry-After": format_datetime(when)
        })

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(RateLimitError):
        await provider.generate(_request())
    assert 0 < provider.cooldown_remaining() <= 6


async def test_retry_after_is_clamped_to_configured_ceiling(monkeypatch):
    def behaviour(url, **kwargs):
        return _Response(status_code=429, headers={"Retry-After": "99999"})

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(RateLimitError):
        await provider.generate(_request())
    assert provider.cooldown_remaining() <= 121, "ceiling=120s (+slack)"


async def test_rate_limit_headers_are_read(monkeypatch, caplog):
    def behaviour(url, **kwargs):
        return _Response(status_code=429, headers={
            "Retry-After": "3",
            "X-RateLimit-Limit": "50",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1735689600",
        })

    provider = _provider(monkeypatch, behaviour)
    with caplog.at_level("INFO", logger="app.ai.openrouter"):
        with pytest.raises(RateLimitError):
            await provider.generate(_request())
    joined = caplog.text
    assert "x-ratelimit-limit=50" in joined
    assert "remaining=0" in joined
    assert "reset=1735689600" in joined


async def test_daily_quota_seeded_from_key_endpoint(monkeypatch):
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 49, "limit": 50}}
    })

    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour, key_response=key_resp)
    # health_check ran inside __init__ and seeded state from /key
    assert provider._daily_limit == 50
    assert provider._daily_used == 49
    assert provider.request_count == 0, "lifetime stats stay separate from quota"
    assert provider.is_available is True  # one request left

    await provider.generate(_request())
    assert provider._daily_exhausted is True  # 50/50 used
    assert provider.request_count == 1, "lifetime stats counted the response"
    assert provider.is_available is False
    with pytest.raises(RateLimitError, match="Daily free quota exhausted"):
        await provider.generate(_request())


async def test_utc_rollover_reopens_quota_and_requests_succeed(monkeypatch):
    """Seed to the daily limit, roll the UTC day, and prove the provider
    becomes available again, a new request succeeds, and yesterday's count
    does not block the new day (while lifetime stats stay separate)."""
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 49, "limit": 50}}
    })

    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour, key_response=key_resp)
    await provider.generate(_request())
    assert provider._daily_exhausted is True  # 50/50 used on day 1

    # simulate the UTC day rolling over
    _arrange_quota_state(provider, date="2000-01-01")
    assert provider.is_available is True
    assert provider._daily_exhausted is False
    assert provider._daily_used == 0

    await provider.generate(_request())
    assert provider._daily_used == 1, "only today's usage counts"
    assert provider.request_count == 2, "lifetime stats are separate, not blocking"
    assert provider.is_available is True


async def test_quota_exhaustion_routes_gateway_to_local(monkeypatch):
    provider = _provider(monkeypatch, lambda *a, **k: _Response())
    _arrange_quota_state(provider, exhausted=True)
    gateway = AIGateway(
        _cfg(), provider=provider, local=LocalProvider(), telemetry=AITelemetry(),
    )
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"
    assert response.metadata.model == "deterministic-local"
    assert response.text


# --------------------------------------------------------------------------- #
# Response / error handling through the chain                                 #
# --------------------------------------------------------------------------- #
async def test_empty_content_is_never_a_success(monkeypatch):
    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("", model="openrouter/free"))

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError, match="empty content"):
        await provider.generate(_request())


async def test_malformed_json_is_rejected(monkeypatch):
    def behaviour(url, **kwargs):
        return _Response(raw=b"{not json at all", payload=None)

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError, match="Malformed JSON"):
        await provider.generate(_request())


async def test_transient_5xx_then_success_bounded_retry(monkeypatch):
    """A 503 is transient: the gateway retries once and OpenRouter then
    answers via the native models chain; the retry count reaches telemetry."""
    outcomes = [_Response(status_code=503), _Response(payload=_ok_payload("recovered"))]
    state = {"n": 0}
    payloads = []

    def behaviour(url, **kwargs):
        if "/key" in url:
            return _Response(payload={"data": {}})
        payloads.append(kwargs.get("json") or {})
        r = outcomes[min(state["n"], len(outcomes) - 1)]
        state["n"] += 1
        return r

    provider = _provider(
        monkeypatch, behaviour,
        openrouter_fallback_models=("nvidia/nemotron-3.5-lightning:free",),
    )
    gateway = AIGateway(
        _cfg(), provider=provider, local=LocalProvider(), telemetry=AITelemetry(),
    )
    request = _request()
    response = await gateway.generate(request)
    assert response.text == "recovered"
    assert response.metadata.success is True
    assert response.metadata.retry_count == 1
    assert response.metadata.model == "google/gemma-4-31b-it:free"
    # both attempts carried the native models array, never `model`
    for payload in payloads:
        assert payload["models"] == [
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3.5-lightning:free",
        ]
        assert "model" not in payload


async def test_gateway_falls_back_to_local_when_all_models_fail(monkeypatch):
    def behaviour(url, **kwargs):
        return _Response(status_code=429, headers={"Retry-After": "1"})

    provider = _provider(monkeypatch, behaviour)
    gateway = AIGateway(
        _cfg(), provider=provider, local=LocalProvider(), telemetry=AITelemetry(),
    )
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"
    assert response.metadata.success is True


# --------------------------------------------------------------------------- #
# Phase 6: safe public errors                                                 #
# --------------------------------------------------------------------------- #
def test_provider_error_details_never_reach_users():
    err = AIProviderError("OpenRouter 400: upstream body mentioned sk-or-v1-secret")
    message = public_message(err)
    assert "OpenRouter" not in message
    assert "400" not in message
    assert "sk-or-v1" not in message
    assert "الذكاء الاصطناعي" in message


def test_validation_error_still_reaches_users():
    assert public_message(ValidationError("الاسم مطلوب")) == "الاسم مطلوب"


def test_rate_limit_error_gets_static_message():
    message = public_message(RateLimitError("Daily free quota exhausted (50/50)"))
    assert "50/50" not in message
    assert "الحد المسموح" in message


# --------------------------------------------------------------------------- #
# Atomic daily-quota reservation                                              #
# --------------------------------------------------------------------------- #
async def test_concurrent_requests_cannot_exceed_remaining_quota(monkeypatch):
    """More concurrent requests than remaining slots: only the available
    slots are sent; the rest are refused before any HTTP I/O."""
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 0, "limit": 2}}
    })
    post_calls = {"n": 0}

    def behaviour(url, **kwargs):
        post_calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour, key_response=key_resp, min_request_interval=0.0,
    )
    results = await asyncio.gather(*[
        provider.generate(_request(user_id=i)) for i in range(5)
    ], return_exceptions=True)

    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, RateLimitError)]
    assert len(ok) == 2, "only the remaining slots may be sent"
    assert len(refused) == 3, "everything beyond the quota is refused"
    assert post_calls["n"] == 2, "no HTTP request may bypass the reservation"
    assert provider._daily_used == 2


async def test_uncertain_outcomes_consume_the_reserved_slot(monkeypatch):
    """Conservative accounting: a connection failure that may or may not have
    reached the provider keeps its reservation (no under-counting)."""
    def behaviour(url, **kwargs):
        raise requests.exceptions.ConnectionError("reset before response")

    provider = _provider(monkeypatch, behaviour)
    _arrange_quota_state(provider, limit=1)
    with pytest.raises(AIProviderError):
        await provider.generate(_request())
    assert provider._daily_used == 1, "uncertain outcome stays consumed"
    with pytest.raises(RateLimitError, match="[Dd]aily.*quota"):
        await provider.generate(_request())


def test_status_text_uses_daily_counters(monkeypatch):
    class _DailyProvider(AIProvider):
        name = "openrouter"
        request_count = 999  # lifetime noise must not leak into the view
        daily_used = 10
        daily_limit = 100

        @property
        def is_available(self):
            return True

        async def generate(self, request):
            return "x"

    gateway = AIGateway(
        _cfg(), provider=_DailyProvider(), local=LocalProvider(),
        telemetry=AITelemetry(),
    )
    text = gateway.status_text()
    assert "10/100" in text, "daily counters drive the view"
    assert "999" not in text, "lifetime statistics never leak into the quota view"


def test_status_text_falls_back_for_duck_typed_providers(monkeypatch):
    class _LegacyProvider(AIProvider):
        name = "openrouter"
        request_count = 7

        @property
        def is_available(self):
            return True

        async def generate(self, request):
            return "x"

    gateway = AIGateway(
        _cfg(), provider=_LegacyProvider(), local=LocalProvider(),
        telemetry=AITelemetry(),
    )
    assert "7/" in gateway.status_text()


# --------------------------------------------------------------------------- #
# Free-only primary model                                                     #
# --------------------------------------------------------------------------- #
def test_paid_primary_rejected_in_development(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("AUSTRO_ENVIRONMENT", "development")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-5-mini")
    from app.config.settings import load_settings
    with pytest.raises(ConfigurationError, match="OPENROUTER_MODEL"):
        load_settings()


def test_paid_primary_rejected_in_staging(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("AUSTRO_ENVIRONMENT", "staging")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet")
    from app.config.settings import load_settings
    with pytest.raises(ConfigurationError, match="OPENROUTER_MODEL"):
        load_settings()


def test_paid_primary_rejected_in_production(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("AUSTRO_ENVIRONMENT", "production")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-5-mini")
    from app.config.settings import load_settings
    with pytest.raises(ConfigurationError, match="OPENROUTER_MODEL"):
        load_settings()


def test_free_primary_accepted_and_default_preserved(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    from app.config.settings import load_settings
    settings = load_settings()
    assert settings.openrouter_model == "google/gemma-4-31b-it:free"

    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    assert load_settings().openrouter_model == "google/gemma-4-26b-a4b-it:free"


def test_openrouter_free_router_accepted_as_primary(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    from app.config.settings import load_settings
    assert load_settings().openrouter_model == "openrouter/free"


def test_structural_free_id_validation_rejects_malformed_ids():
    from app.config.settings import _is_free_model_id
    for bad in (
        ":free",              # no vendor/model
        "vendor/:free",       # empty model segment
        "vendor/model:paid",  # non-free suffix
        "vendor/model",       # no free suffix at all
        "vendor model:free",  # whitespace
        "vendor/ model:free", # inner whitespace
        "/model:free",        # empty vendor
        "vendor//model:free", # empty middle segment
        "Vendor/Model:FREE",  # suffix is case-sensitive
    ):
        assert not _is_free_model_id(bad), bad
    for good in (
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
        "vendor.name/model.name-v2:free",
        "openrouter/free",
    ):
        assert _is_free_model_id(good), good


def test_malformed_primary_rejected_through_settings_loader(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
    monkeypatch.setenv("OPENROUTER_MODEL", ":free")
    from app.config.settings import load_settings
    with pytest.raises(ConfigurationError, match="OPENROUTER_MODEL"):
        load_settings()


# --------------------------------------------------------------------------- #
# Quota synchronization across rollovers and reconnects                       #
# --------------------------------------------------------------------------- #
async def test_midnight_rollover_then_reconnect_preserves_today_usage(monkeypatch):
    """Yesterday's exhausted state must not wipe a reconnect's /key snapshot
    of TODAY's usage: the quota date is synchronized with the seeding."""
    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour)
    # 1) yesterday: quota exhausted
    _arrange_quota_state(provider, date="2000-01-01", used=50, limit=50,
                         exhausted=True)

    # 2) the UTC date has advanced to today
    today = provider._utc_today()
    monkeypatch.setattr(
        OpenRouterProvider, "_utc_today", staticmethod(lambda: today)
    )

    # 3) reconnect: /key reports today's non-zero usage
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 3, "limit": 50}}
    })
    monkeypatch.setattr("app.ai.openrouter.requests.get", lambda url, **k: key_resp)
    assert provider.reconnect() is True

    # 4+5) today's usage is preserved (not reset by the stale date) and
    # quota enforcement uses the newly seeded values
    assert provider._quota_date == today
    assert provider._daily_used == 3
    assert provider._daily_limit == 50
    assert provider._daily_exhausted is False
    assert provider.is_available is True

    # 6) the next generation bills against the seeded counter
    await provider.generate(_request())
    assert provider._daily_used == 4


async def test_reconnect_after_exhaustion_restores_availability(monkeypatch):
    """A prematurely exhausted provider recovers when /key reports usage
    below the daily limit; the next request succeeds and counts."""
    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour)
    # 1) exhausted (e.g. conservative over-counting)
    _arrange_quota_state(provider, used=50, limit=50, exhausted=True,
                         cooldown_until=0.0)
    assert provider.is_available is False

    # 2+3) reconnect with /key reporting 30/50
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 30, "limit": 50}}
    })
    monkeypatch.setattr("app.ai.openrouter.requests.get", lambda url, **k: key_resp)
    assert provider.reconnect() is True

    # 4) available again (no cooldown, quota below the limit)
    assert provider.is_available is True
    assert provider._daily_exhausted is False
    assert provider._daily_used == 30

    # 5) the next request succeeds and the daily counter increments
    await provider.generate(_request())
    assert provider._daily_used == 31
    assert provider.request_count == 1


async def test_reconnect_keeps_exhaustion_when_key_confirms_it(monkeypatch):
    """When /key confirms the quota remains exhausted, reconnect must not
    resurrect the provider."""
    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour)
    _arrange_quota_state(provider, used=50, limit=50, exhausted=True)

    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 50, "limit": 50}}
    })
    monkeypatch.setattr("app.ai.openrouter.requests.get", lambda url, **k: key_resp)
    assert provider.reconnect() is True  # key itself is healthy
    assert provider._daily_exhausted is True
    assert provider.is_available is False
    with pytest.raises(RateLimitError):
        await provider.generate(_request())


def test_malformed_key_snapshot_does_not_reset_valid_state(monkeypatch):
    """A /key response without usable quota fields must leave the existing
    account state exactly as it was."""
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=50, exhausted=False)

    for payload in (
        {"data": {}},                                    # block missing
        {"data": {"free_model_daily_requests": "junk"}},  # wrong shape
        {"data": {"free_model_daily_requests": {"used": "x", "limit": None}}},
        {"error": {"message": "server hiccup"}},          # no data at all
    ):
        provider._parse_key_quota(payload)
        assert provider._daily_used == 17, payload
        assert provider._daily_limit == 50, payload
        assert provider._daily_exhausted is False, payload


# --------------------------------------------------------------------------- #
# Reservation placement: after pacing, immediately before HTTP                #
# --------------------------------------------------------------------------- #
async def test_cancellation_during_pacing_consumes_no_slot(monkeypatch):
    """A task cancelled while waiting in the pacing sleep must not burn a
    daily slot it never used."""
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 0, "limit": 5}}
    })
    post_calls = {"n": 0}

    def behaviour(url, **kwargs):
        post_calls["n"] += 1
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour, key_response=key_resp, min_request_interval=30.0,
    )
    # a first send stamps last_request_time, so the NEXT request parks in
    # the pacing sleep (min_request_interval=30s)
    await provider.generate(_request())
    assert provider._daily_used == 1 and post_calls["n"] == 1

    task = asyncio.ensure_future(provider.generate(_request()))
    await asyncio.sleep(0.05)  # the task is now parked inside the pacing sleep
    assert provider._daily_used == 1, "no reservation while parked in pacing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider._daily_used == 1, "cancellation during pacing frees the slot"
    assert post_calls["n"] == 1

    # the slot is intact: a normal request (pacing disabled) still goes through
    provider._settings = dataclasses.replace(
        provider._settings, min_request_interval=0.0
    )
    await provider.generate(_request())
    assert post_calls["n"] == 2
    assert provider._daily_used == 2


async def test_concurrent_requests_stay_capped_after_pacing(monkeypatch):
    """With pacing enabled, reservations still serialize under the lock:
    concurrent tasks can never send more than the remaining slots."""
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 0, "limit": 2}}
    })
    post_calls = {"n": 0}

    def behaviour(url, **kwargs):
        post_calls["n"] += 1
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour, key_response=key_resp, min_request_interval=0.02,
    )
    results = await asyncio.gather(*[
        provider.generate(_request(user_id=i)) for i in range(4)
    ], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, RateLimitError)]
    assert len(ok) == 2 and len(refused) == 2
    assert post_calls["n"] == 2
    assert provider._daily_used == 2


# --------------------------------------------------------------------------- #
# All-or-nothing /key snapshot application                                    #
# --------------------------------------------------------------------------- #
def test_key_snapshot_partial_fields_preserve_existing_state(monkeypatch):
    """Incomplete snapshots (one field valid, the other missing/malformed)
    must not mix new usage with an old limit or vice versa."""
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=50, exhausted=False)
    before = (provider._quota_date, 17, 50, False)

    cases = [
        {"data": {"free_model_daily_requests": {"used": 5}}},                 # valid used, missing limit
        {"data": {"free_model_daily_requests": {"limit": 80}}},               # missing used, valid limit
        {"data": {"free_model_daily_requests": {"used": 5, "limit": "x"}}},   # malformed limit
        {"data": {"free_model_daily_requests": {"used": None, "limit": 80}}}, # malformed used
        {"data": {"free_model_daily_requests": {"used": -3, "limit": 80}}},   # negative usage
        {"data": {"free_model_daily_requests": {"used": 5, "limit": 0}}},     # non-positive limit
        {"data": {"free_model_daily_requests": {"used": True, "limit": 80}}}, # boolean usage
        {"data": {"free_model_daily_requests": {"used": 5, "limit": True}}},  # boolean limit
    ]
    for payload in cases:
        provider._parse_key_quota(payload)
        assert (provider._quota_date, provider._daily_used,
                provider._daily_limit, provider._daily_exhausted) == before, payload


def test_key_snapshot_complete_valid_replaces_previous_state(monkeypatch):
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=20, exhausted=True)

    today = provider._utc_today()
    provider._parse_key_quota({"data": {
        "free_model_daily_requests": {"used": 4, "limit": 50}
    }})
    assert provider._quota_date == today
    assert provider._daily_used == 4
    assert provider._daily_limit == 50
    assert provider._daily_exhausted is False

    # and an exhausting complete snapshot flips the flag
    provider._parse_key_quota({"data": {
        "free_model_daily_requests": {"used": 50, "limit": 50}
    }})
    assert provider._daily_exhausted is True


def test_key_snapshot_booleans_are_never_numbers(monkeypatch):
    """bool is an int subclass; quota fields must reject it explicitly."""
    assert not OpenRouterProvider._quota_number(True)
    assert not OpenRouterProvider._quota_number(False)


def test_key_snapshot_legacy_numeric_representation_validated_as_a_pair(monkeypatch):
    """Legacy numeric block (limit) + separate usage field applies only
    when BOTH validate; otherwise the old state survives."""
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=50, exhausted=False)

    # both valid -> applied
    provider._parse_key_quota({"data": {
        "free_model_daily_requests": 80, "free_model_daily_usage": 9,
    }})
    assert (provider._daily_used, provider._daily_limit) == (9, 80)

    # legacy limit without a usable usage -> whole snapshot rejected
    provider._parse_key_quota({"data": {"free_model_daily_requests": 60}})
    assert (provider._daily_used, provider._daily_limit) == (9, 80)

    # boolean legacy usage -> rejected
    provider._parse_key_quota({"data": {
        "free_model_daily_requests": 60, "free_model_daily_usage": True,
    }})
    assert (provider._daily_used, provider._daily_limit) == (9, 80)


# --------------------------------------------------------------------------- #
# Lock contract: rollover racing snapshots and reservations                   #
# --------------------------------------------------------------------------- #
async def test_rollover_racing_snapshot_and_reservation_stays_consistent(monkeypatch):
    """Deterministic mixed traffic across a simulated UTC boundary: the
    final state must always be internally consistent (correct date, usage
    within the limit or flagged exhausted), and the run must complete -
    proving the lock contract has no deadlock and no torn transitions."""
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 1, "limit": 5}}
    })
    state = {"n": 0}

    def behaviour(url, **kwargs):
        state["n"] += 1
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour, key_response=key_resp, min_request_interval=0.0,
    )
    real_utc_today = OpenRouterProvider._utc_today
    sequence = ["2000-01-01", "2000-01-01", "2000-01-01",
                "2000-01-02", "2000-01-02", "2000-01-02"]
    flipping = {"i": 0}

    def flipping_utc_today():
        i = flipping["i"]
        flipping["i"] += 1
        return sequence[i % len(sequence)]

    monkeypatch.setattr(
        OpenRouterProvider, "_utc_today", staticmethod(flipping_utc_today)
    )

    async def mixed_worker(i):
        for _ in range(20):
            _ = provider.daily_used
            _ = provider.is_available
            provider._parse_key_quota({"data": {
                "free_model_daily_requests": {"used": i % 3, "limit": 5}
            }})
            try:
                await asyncio.wait_for(provider.generate(_request()), timeout=2)
            except RateLimitError:
                pass
            except AIProviderError:
                pass
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(*[
        mixed_worker(i) for i in range(4)
    ]), timeout=30)

    today = real_utc_today()
    assert provider._quota_date in (today, "2000-01-02")  # never yesterday's state
    if provider._quota_date == today:
        assert provider._daily_used >= 0
        if provider._daily_used >= provider._effective_daily_limit():
            assert provider._daily_exhausted is True
        else:
            assert provider._daily_exhausted is False


# --------------------------------------------------------------------------- #
# Reconnect with an in-flight request                                         #
# --------------------------------------------------------------------------- #
async def test_reconnect_during_inflight_request_documents_reconciliation(monkeypatch):
    """Policy: a /key snapshot applied while a request is in flight replaces
    the local reservation with the account's truth. The reservation-time cap
    still held (no over-send happened), and post-reconnect enforcement uses
    the seeded values. Exact reconciliation with the external account is NOT
    claimed: if OpenRouter had not yet counted the in-flight request, local
    usage may briefly under-count until the next snapshot."""
    import threading

    release = threading.Event()
    key_resp = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 10, "limit": 50}}
    })

    def behaviour(url, **kwargs):
        release.wait(timeout=5)
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour, key_response=key_resp)

    # __init__ seeds 10/50 from /key, then 1+2) the task reserves one slot
    # and holds the HTTP request in flight.
    task = asyncio.ensure_future(provider.generate(_request()))
    await asyncio.sleep(0.1)
    assert provider._daily_used == 11, "seeded 10 + the in-flight reservation"

    # 3) reconnect re-applies the /key snapshot while the request is in
    #    flight: the account snapshot replaces the local counter (the
    #    in-flight reservation is reconciled away - documented policy).
    assert provider.reconnect() is True
    assert provider._daily_used == 10
    assert provider._quota_date == provider._utc_today()
    assert provider._daily_exhausted is False

    # 4) release the request
    release.set()
    text = await asyncio.wait_for(task, timeout=10)
    assert text == "ok"

    # 5) no local concurrency guarantee was violated: the in-flight send was
    #    admitted when slots remained, and post-reconnect enforcement uses
    #    the seeded account values (the next send still reserves a slot).
    assert provider.is_available is True
    await provider.generate(_request())
    assert provider._daily_used == 11


# --------------------------------------------------------------------------- #
# Finding 1: generate() vs concurrent state transitions (deterministic)       #
# --------------------------------------------------------------------------- #
async def test_generate_racing_key_snapshot_is_arbitrated_by_reservation(monkeypatch):
    """A /key snapshot landing between the advisory check and the reservation
    is handled by the authoritative gate: exhausting the quota in the race
    window means the request is refused by the reservation, never admitted."""
    init_key = _Response(payload={
        "data": {"free_model_daily_requests": {"used": 0, "limit": 2}}
    })  # healthy at advisory time (0/2)
    racing_key = {"data": {"free_model_daily_requests": {"used": 2, "limit": 2}}}

    def behaviour(url, **kwargs):
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(monkeypatch, behaviour, key_response=init_key)

    async def racing_pacing():
        # ...the snapshot lands in the race window, exhausting the quota
        provider._parse_key_quota(racing_key)

    monkeypatch.setattr(provider, "_rate_limit", racing_pacing)
    with pytest.raises(RateLimitError, match="Daily quota"):
        await provider.generate(_request())


async def test_generate_racing_utc_rollover_is_arbitrated_by_reservation(monkeypatch):
    """A UTC rollover landing in the race window - specifically between the
    advisory snapshot and the authoritative reservation - re-opens the quota.

    The mocked clock says 2030-01-01 during `generate()`'s initial
    `_maybe_reset_daily()` and `_daily_exhausted_snapshot()` (so neither may
    perform the rollover), and flips to 2030-01-02 inside the `_rate_limit`
    hook. The real `_reserve_daily_slot()` must then detect the new UTC date
    under `_state_lock`, reset the previous day's usage, and reserve exactly
    one slot. If the rollover happened any earlier, or the reservation
    skipped its locked date recheck, the assertions below fail."""
    clock = {"today": "2030-01-01"}
    monkeypatch.setattr(
        OpenRouterProvider, "_utc_today", staticmethod(lambda: clock["today"])
    )

    post_calls = {"n": 0}

    def behaviour(url, **kwargs):
        post_calls["n"] += 1
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    provider = _provider(
        monkeypatch, behaviour, key_response=_Response(payload={"data": {}}),
    )
    # deterministic initial quota state, arranged under the state lock
    with provider._state_lock:
        provider._quota_date = "2030-01-01"
        provider._daily_limit = 2
        provider._daily_used = 1
        provider._daily_exhausted = False

    async def rollover_pacing():
        # the UTC day flips strictly inside the race window
        clock["today"] = "2030-01-02"

    monkeypatch.setattr(provider, "_rate_limit", rollover_pacing)

    request = _request()
    assert await provider.generate(request) == "ok"
    assert provider._quota_date == "2030-01-02", "the rollover applied in-window"
    assert provider._daily_used == 1, "exactly one slot reserved on the new day"
    assert provider._daily_exhausted is False
    assert post_calls["n"] == 1, "exactly one HTTP request was sent"


async def test_exhaustion_in_race_window_is_refused_by_reservation(monkeypatch):
    """Exhaustion appearing inside the race window must be caught by the
    authoritative reservation, not only by the advisory fast-fail. The state
    transition follows the real production path: `_parse_key_quota()` with a
    valid exhausted snapshot (used=2, limit=2), which acquires `_state_lock`
    itself instead of bypassing it."""
    post_calls = {"n": 0}

    def behaviour(url, **kwargs):
        post_calls["n"] += 1
        return _Response(payload=_ok_payload("ok", model="google/gemma-4-31b-it:free"))

    # healthy at init and at advisory time (1/2 used)
    provider = _provider(
        monkeypatch, behaviour,
        key_response=_Response(payload={
            "data": {"free_model_daily_requests": {"used": 1, "limit": 2}}
        }),
    )
    assert provider._daily_exhausted is False

    async def exhausting_pacing():
        # the exhausting snapshot lands strictly inside the race window
        provider._parse_key_quota({"data": {
            "free_model_daily_requests": {"used": 2, "limit": 2}
        }})

    monkeypatch.setattr(provider, "_rate_limit", exhausting_pacing)
    with pytest.raises(RateLimitError, match="Daily quota"):
        await provider.generate(_request())
    assert post_calls["n"] == 0, "no HTTP request may bypass the reservation"
    assert provider._daily_exhausted is True, "the quota remains exhausted"
    assert provider._daily_used == 2, "no additional slot was reserved"


# --------------------------------------------------------------------------- #
# Finding 2: strict numeric validation                                        #
# --------------------------------------------------------------------------- #
def test_quota_number_accepts_only_integer_counts():
    import math
    accept = (0, 1, 50, 2**40, 5.0, 0.0)
    for v in accept:
        assert OpenRouterProvider._quota_number(v), v
    reject = (True, False, 5.9, -0.5, float("nan"), float("inf"),
              float("-inf"), "5", None, [5], {"used": 1}, 5 + 0j)
    for v in reject:
        assert not OpenRouterProvider._quota_number(v), repr(v)
    # sanity: the accepted integral float survives int() losslessly
    assert int(5.0) == 5 and math.isfinite(5.0)


def test_snapshot_fractional_and_nonfinite_are_rejected_wholesale(monkeypatch):
    """Fractional usage/limit and NaN/Infinity snapshots are rejected as a
    pair: (date, used, limit, exhausted) must remain exactly as before."""
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=50, exhausted=False)
    before = (provider._quota_date, 17, 50, False)

    cases = [
        {"data": {"free_model_daily_requests": {"used": 5.9, "limit": 80}}},
        {"data": {"free_model_daily_requests": {"used": 5, "limit": 80.5}}},
        {"data": {"free_model_daily_requests": {"used": float("nan"), "limit": 80}}},
        {"data": {"free_model_daily_requests": {"used": 5, "limit": float("inf")}}},
        {"data": {"free_model_daily_requests": {"used": float("-inf"), "limit": 80}}},
        {"data": {"free_model_daily_requests": {
            "used": 5.0, "limit": 80.0}}},  # finite integral floats are VALID
    ]
    for payload in cases[:-1]:
        provider._parse_key_quota(payload)
        assert (provider._quota_date, provider._daily_used,
                provider._daily_limit, provider._daily_exhausted) == before, payload

    # the valid integral-float snapshot IS applied (intentional support)
    provider._parse_key_quota(cases[-1])
    assert (provider._daily_used, provider._daily_limit, provider._daily_exhausted) == (5, 80, False)


def test_snapshot_negative_usage_and_zero_limit_rejected(monkeypatch):
    provider = _provider(monkeypatch, lambda url, **k: _Response())
    _arrange_quota_state(provider, used=17, limit=50, exhausted=False)
    before = (provider._quota_date, 17, 50, False)

    for payload in (
        {"data": {"free_model_daily_requests": {"used": -1, "limit": 80}}},
        {"data": {"free_model_daily_requests": {"used": 5, "limit": -80}}},
        {"data": {"free_model_daily_requests": {"used": 5, "limit": 0}}},
    ):
        provider._parse_key_quota(payload)
        assert (provider._quota_date, provider._daily_used,
                provider._daily_limit, provider._daily_exhausted) == before, payload
