"""AUSTRO AI - OpenRouter provider + gateway failover regressions.

All HTTP is mocked at `app.ai.openrouter.requests`; no network and no real
API key are required. Provider behaviour covers: success, missing key,
401/403, 429 with/without Retry-After, 5xx, timeout, connection failure,
malformed JSON and empty content. Gateway behaviour covers the failover
OpenRouter -> local -> safe message, bounded retries,
no-retry-on-auth, and telemetry attribution (provider/model), plus secret
redaction in raised errors.
"""

import asyncio
import dataclasses
import os

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["OPENROUTER_API_KEY"] = ""

import pytest
import requests

from app.ai.gateway import AIGateway
from app.ai.openrouter import OpenRouterProvider
from app.config.settings import settings as base_settings
from app.core.errors import AIAuthenticationError, AIProviderError, RateLimitError
from app.domain.ai import AIRequest

_OR_KEY = "sk-or-test-key-0000000000"

_DEFAULTS = dict(
    openrouter_api_key=_OR_KEY,
    openrouter_model="google/gemma-4-31b-it:free",
    openrouter_api_url="https://openrouter.example/api/v1",
    ai_request_timeout=5.0,
    ai_healthcheck_timeout=1.0,
    min_request_interval=0.0,
    max_requests_per_day=100,
)


def _cfg(**overrides):
    return dataclasses.replace(base_settings, **{**_DEFAULTS, **overrides})


def _request():
    return AIRequest(capability="chat", prompt="مرحباً")


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None,
                 raise_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "or-ok"}}]
        }
        self._raise_json = raise_json
        self.headers = headers or {}

    def json(self):
        if self._raise_json:
            raise ValueError("malformed json")
        return self._payload


def _or_payload(text="or-ok"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _provider(monkeypatch, behaviour, **cfg_overrides):
    """Available OpenRouter provider; `behaviour(call_index)` returns a response."""
    monkeypatch.setattr("app.ai.openrouter.requests.get", lambda *a, **k: _Response())
    monkeypatch.setattr("app.ai.openrouter.requests.post", behaviour)
    provider = OpenRouterProvider(_cfg(**cfg_overrides))
    assert provider.is_available is True, "health check should have succeeded"
    return provider


# ============================ provider: availability ============================


def test_missing_key_leaves_provider_inert_and_offline():
    provider = OpenRouterProvider(_cfg(openrouter_api_key=""))
    assert provider.is_available is False
    assert provider.name == "openrouter"


def test_short_key_is_treated_as_missing():
    provider = OpenRouterProvider(_cfg(openrouter_api_key="short"))
    assert provider.is_available is False


def test_health_check_401_disables_provider(monkeypatch):
    monkeypatch.setattr(
        "app.ai.openrouter.requests.get",
        lambda *a, **k: _Response(status_code=401),
    )
    provider = OpenRouterProvider(_cfg())
    assert provider.is_available is False


def test_health_check_connection_error_disables_provider(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr("app.ai.openrouter.requests.get", boom)
    provider = OpenRouterProvider(_cfg())
    assert provider.is_available is False


# ============================ provider: generate paths ============================


@pytest.mark.asyncio
async def test_success_returns_text_and_counts_request(monkeypatch):
    calls = {"n": 0}

    def behaviour(*args, **kwargs):
        calls["n"] += 1
        return _Response(payload=_or_payload("جواب"))

    provider = _provider(monkeypatch, behaviour)
    text = await provider.generate(_request())
    assert text == "جواب"
    assert provider.request_count == 1


@pytest.mark.asyncio
async def test_payload_shape_and_auth_header(monkeypatch):
    seen = {}

    def behaviour(url, headers=None, json=None, timeout=None, **kwargs):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response()

    provider = _provider(monkeypatch, behaviour)
    await provider.generate(AIRequest(
        capability="chat", prompt="hi", system_prompt="sys",
        max_tokens=99999, temperature=0.2,
    ))

    assert seen["url"].endswith("/chat/completions")
    assert seen["headers"]["Authorization"] == f"Bearer {_OR_KEY}"
    body = seen["json"]
    assert body["model"] == "google/gemma-4-31b-it:free"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["max_tokens"] <= 2048  # conservative output cap enforced


@pytest.mark.asyncio
async def test_401_raises_authentication_error_and_disables(monkeypatch):
    def behaviour(*args, **kwargs):
        return _Response(status_code=401)

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIAuthenticationError):
        await provider.generate(_request())
    assert provider.is_available is False


@pytest.mark.asyncio
async def test_403_raises_authentication_error(monkeypatch):
    def behaviour(*args, **kwargs):
        return _Response(status_code=403)

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIAuthenticationError):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_429_raises_rate_limit_with_retry_after(monkeypatch):
    def behaviour(*args, **kwargs):
        return _Response(
            status_code=429,
            headers={"Retry-After": "7"},
        )

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(RateLimitError, match="Retry-After=7"):
        await provider.generate(_request())
    assert provider.is_available is False


@pytest.mark.asyncio
async def test_429_without_retry_after_still_reports_quota(monkeypatch):
    def behaviour(*args, **kwargs):
        return _Response(status_code=429)

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(RateLimitError, match="no Retry-After"):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_5xx_is_transient_and_keeps_provider_available(monkeypatch):
    def behaviour(*args, **kwargs):
        return _Response(status_code=503)

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError, match="503"):
        await provider.generate(_request())
    assert provider.is_available is True, "5xx is transient: no permanent disable"


@pytest.mark.asyncio
async def test_timeout_is_transient(monkeypatch):
    def behaviour(*args, **kwargs):
        raise requests.exceptions.Timeout("slow")

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError, match="timeout"):
        await provider.generate(_request())
    assert provider.is_available is True


@pytest.mark.asyncio
async def test_connection_failure_raises_provider_error(monkeypatch):
    def behaviour(*args, **kwargs):
        raise requests.exceptions.ConnectionError("reset")

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_malformed_json_raises_provider_error(monkeypatch):
    provider = _provider(
        monkeypatch, lambda *a, **k: _Response(raise_json=True)
    )
    with pytest.raises(AIProviderError, match="Malformed JSON"):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_empty_content_raises_provider_error(monkeypatch):
    provider = _provider(
        monkeypatch, lambda *a, **k: _Response(payload={"choices": [{"message": {"content": "   "}}]})
    )
    with pytest.raises(AIProviderError, match="empty content"):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_missing_choices_raises_provider_error(monkeypatch):
    provider = _provider(monkeypatch, lambda *a, **k: _Response(payload={"choices": []}))
    with pytest.raises(AIProviderError, match="no choices"):
        await provider.generate(_request())


@pytest.mark.asyncio
async def test_error_messages_never_contain_the_key(monkeypatch):
    """A provider that echoes the key back must not leak it into the error."""

    def behaviour(*args, **kwargs):
        return _Response(
            status_code=400,
            payload={"error": {"message": f"bad key {_OR_KEY} in request"}},
        )

    provider = _provider(monkeypatch, behaviour)
    with pytest.raises(AIProviderError) as excinfo:
        await provider.generate(_request())
    assert _OR_KEY not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


@pytest.mark.asyncio
async def test_daily_quota_disables_and_raises_rate_limit(monkeypatch):
    provider = _provider(monkeypatch, lambda *a, **k: _Response())
    provider.request_count = provider._settings.max_requests_per_day
    with pytest.raises(RateLimitError, match="Daily quota"):
        await provider.generate(_request())
    assert provider.is_available is False


# ============================ gateway: OpenRouter-only ============================

class _FakeOpenRouter:
    """Duck-typed OpenRouter provider for gateway behavior tests."""

    name = "openrouter"
    model = "test/openrouter-model"
    request_count = 0

    def __init__(self, behaviour=None, available=True):
        self._behaviour = behaviour
        self._available = available
        self.calls = 0
        self.reconnect_calls = 0

    @property
    def is_available(self):
        return self._available

    async def generate(self, request):
        self.calls += 1
        if self._behaviour is not None:
            return self._behaviour(self)
        return "openrouter-ok"

    def reconnect(self):
        self.reconnect_calls += 1
        return self._available


def _gateway(monkeypatch, primary=None, behaviour=None, **cfg_overrides):
    if primary is None:
        monkeypatch.setattr(
            "app.ai.openrouter.requests.get", lambda *a, **k: _Response()
        )
        if behaviour is not None:
            monkeypatch.setattr("app.ai.openrouter.requests.post", behaviour)
            primary = OpenRouterProvider(_cfg(**cfg_overrides))
            assert primary.is_available
        else:
            primary = _FakeOpenRouter(available=False)
    return AIGateway(settings_=_cfg(**cfg_overrides), provider=primary)


@pytest.mark.asyncio
async def test_openrouter_primary_success_records_provider(monkeypatch):
    gateway = _gateway(monkeypatch, _FakeOpenRouter())
    response = await gateway.generate(_request())
    assert response.text == "openrouter-ok"
    assert response.metadata.provider == "openrouter"
    assert response.metadata.model == "test/openrouter-model"


@pytest.mark.asyncio
async def test_openrouter_auth_failure_falls_to_local(monkeypatch):
    def behaviour(*a, **k):
        return _Response(status_code=401)
    gateway = _gateway(monkeypatch, behaviour=behaviour, ai_max_retries=2)
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"
    assert response.text


@pytest.mark.asyncio
async def test_openrouter_429_falls_to_local(monkeypatch):
    def behaviour(*a, **k):
        return _Response(status_code=429, headers={"Retry-After": "5"})
    gateway = _gateway(monkeypatch, behaviour=behaviour)
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"


@pytest.mark.asyncio
async def test_openrouter_transient_failures_are_bounded_then_local(monkeypatch):
    calls = {"n": 0}
    def behaviour(*a, **k):
        calls["n"] += 1
        return _Response(status_code=503)
    gateway = _gateway(
        monkeypatch, behaviour=behaviour, ai_max_retries=2,
        ai_retry_backoff_base=0.0,
    )
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_openrouter_failure_returns_safe_message_when_local_disabled(monkeypatch):
    def behaviour(*a, **k):
        return _Response(status_code=500)
    gateway = _gateway(
        monkeypatch, behaviour=behaviour, ai_max_retries=0,
        ai_retry_backoff_base=0.0, use_local_fallback=False,
    )
    response = await gateway.generate(_request())
    from app.ai.gateway import _UNAVAILABLE_MESSAGE
    assert response.text == _UNAVAILABLE_MESSAGE
    assert response.metadata.success is False
    assert response.metadata.provider == "none"


@pytest.mark.asyncio
async def test_gateway_default_provider_is_openrouter_and_no_key_stays_inert(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("no OpenRouter HTTP call may happen without a key")
    monkeypatch.setattr("app.ai.openrouter.requests.get", no_network)
    monkeypatch.setattr("app.ai.openrouter.requests.post", no_network)
    gateway = AIGateway(settings_=_cfg(openrouter_api_key=""))
    assert isinstance(gateway.provider, OpenRouterProvider)
    assert gateway.provider.is_available is False
    response = await gateway.generate(_request())
    assert response.metadata.provider == "local"


@pytest.mark.asyncio
async def test_reconnect_refreshes_only_openrouter(monkeypatch):
    provider = _FakeOpenRouter(available=True)
    gateway = _gateway(monkeypatch, provider)
    await gateway.reconnect()
    assert provider.reconnect_calls == 1
    assert not hasattr(gateway, "secondary")


@pytest.mark.asyncio
async def test_pacing_spans_concurrent_calls(monkeypatch):
    import time as _time
    timestamps = []
    def behaviour(*args, **kwargs):
        timestamps.append(_time.monotonic())
        return _Response()
    provider = _provider(monkeypatch, behaviour, min_request_interval=0.05)
    await asyncio.gather(*(provider.generate(_request()) for _ in range(4)))
    assert len(timestamps) == 4
    span = max(timestamps) - min(timestamps)
    assert span >= 3 * 0.05 * 0.8, "paced slots must spread, not burst"
