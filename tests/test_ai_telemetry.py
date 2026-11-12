"""AI gateway telemetry contracts (A4).

One JSON record is emitted per completed run, and the in-memory record carries
the same measured values. Provider usage is read from the real OpenRouter
adapter with HTTP mocked; no network calls are made.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import os

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["OPENROUTER_API_KEY"] = ""

import pytest  # noqa: E402

from app.ai.gateway import AIGateway  # noqa: E402
from app.ai.openrouter import OpenRouterProvider  # noqa: E402
from app.ai.telemetry import AITelemetry  # noqa: E402
from app.config.settings import settings as base_settings  # noqa: E402
from app.core.errors import AIProviderError  # noqa: E402
from app.domain.ai import AIRequest  # noqa: E402
from app.security.redaction import RedactingFormatter  # noqa: E402

_KEYS = (
    "run_id",
    "capability",
    "provider",
    "model",
    "latency_ms",
    "success",
    "retry_count",
    "tokens_prompt",
    "tokens_completion",
    "error",
)


def _cfg(**overrides):
    return dataclasses.replace(
        base_settings,
        use_local_fallback=False,
        ai_request_timeout=5.0,
        ai_max_retries=2,
        ai_retry_backoff_base=0.0,
        **overrides,
    )


class _StubProvider:
    name = "stub"
    model = "stub-model"
    is_available = True

    def __init__(self, failures: int = 0, error: BaseException = None,
                 policy: bool = False):
        self.failures = failures
        self.error = error
        self.policy = policy
        self.calls = 0

    async def generate(self, request: AIRequest) -> str:
        self.calls += 1
        if self.policy:
            raise self.error
        if self.calls <= self.failures:
            raise AIProviderError("transient upstream failure")
        request.data["_actual_model"] = "stub-actual"
        request.data["_usage"] = {
            "prompt_tokens": 123456,
            "completion_tokens": 789,
            "cost_usd": 0.0042,
        }
        return "stub answer"


def _gateway(provider, telemetry=None):
    return AIGateway(_cfg(), provider=provider, telemetry=telemetry or AITelemetry())


def _json_records(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "app.ai.telemetry"
    ]


@pytest.mark.asyncio
async def test_success_records_measured_fields_and_one_json_line(caplog):
    caplog.set_level(logging.INFO, logger="app.ai.telemetry")
    gateway = _gateway(_StubProvider())
    response = await gateway.generate(AIRequest(capability="chat", prompt="hello"))

    record = gateway.telemetry.recent()[-1]
    assert record.success is True
    assert record.latency_ms is not None
    assert record.latency_ms >= 0.0
    assert record.provider == "stub"
    assert record.model == "stub-actual"
    assert record.capability == "chat"
    assert record.retry_count == 0
    assert record.error is None
    assert record.tokens_prompt == 123456
    assert record.tokens_completion == 789
    assert record.estimated_cost_usd == pytest.approx(0.0042)
    assert response.metadata.run_id == record.run_id

    lines = _json_records(caplog)
    assert len(lines) == 1
    payload = lines[0]
    for key in _KEYS:
        assert key in payload
    assert payload["run_id"] == record.run_id
    assert payload["provider"] == "stub"
    assert payload["model"] == "stub-actual"
    assert payload["capability"] == "chat"
    assert payload["success"] is True
    assert payload["latency_ms"] == record.latency_ms
    assert payload["tokens_prompt"] == 123456
    assert payload["tokens_completion"] == 789
    assert payload["error"] is None

    # No prompt or response text may enter telemetry.
    assert "hello" not in json.dumps(payload)
    assert "stub answer" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_final_failure_records_error_class_and_retries(caplog):
    caplog.set_level(logging.INFO, logger="app.ai.telemetry")
    provider = _StubProvider(policy=True, error=AIProviderError("upstream broke; prompt=private-user-content api_key=SUPER_SECRET_VALUE"))
    gateway = _gateway(provider)
    response = await gateway.generate(AIRequest(capability="chat", prompt="hello"))

    record = gateway.telemetry.recent()[-1]
    assert record.success is False
    assert record.latency_ms is not None
    assert record.latency_ms >= 0.0
    assert record.error == "AIProviderError"
    assert record.retry_count == 2
    assert record.tokens_prompt is None
    assert record.tokens_completion is None
    assert response.metadata.success is False
    assert provider.calls == 3

    payload = _json_records(caplog)[-1]
    assert payload["success"] is False
    assert payload["error"] == "AIProviderError"
    serialized = json.dumps(payload)
    assert "private-user-content" not in serialized
    assert "SUPER_SECRET_VALUE" not in serialized
    assert payload["retry_count"] == 2


@pytest.mark.parametrize("cost", [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(10 ** 10000, id="integer-overflow"),
])
def test_openrouter_rejects_non_finite_and_overflow_costs(monkeypatch, cost):
    provider = _adapter(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    usage = provider._extract_usage({"usage": {"cost": cost}})
    assert usage["cost_usd"] is None



@pytest.mark.asyncio
async def test_retry_then_success_keeps_the_retry_count(caplog):
    caplog.set_level(logging.INFO, logger="app.ai.telemetry")
    provider = _StubProvider(failures=2)
    gateway = _gateway(provider)
    await gateway.generate(AIRequest(capability="chat", prompt="hello"))

    record = gateway.telemetry.recent()[-1]
    assert record.success is True
    assert record.retry_count == 2
    assert record.tokens_prompt == 123456
    assert provider.calls == 3


# ==================== provider usage extraction (real adapter) ==================== #


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "ok"}}]
        }
        self.headers = {}
        self._raw = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        yield self._raw


def _adapter(monkeypatch, payload):
    monkeypatch.setattr("app.ai.openrouter.requests.get", lambda *a, **k: _Response())
    monkeypatch.setattr(
        "app.ai.openrouter.requests.post",
        lambda *a, **k: _Response(payload=payload),
    )
    return OpenRouterProvider(_cfg(openrouter_api_key="sk-or-test-key-0000000000"))


@pytest.mark.asyncio
async def test_openrouter_usage_is_forwarded_to_the_request(monkeypatch):
    provider = _adapter(monkeypatch, {
        "choices": [{"message": {"content": "ok"}}],
        "model": "actual/model",
        "usage": {
            "prompt_tokens": 123456,
            "completion_tokens": 789,
            "total_tokens": 124245,
            "cost": 0.0042,
        },
    })
    request = AIRequest(capability="chat", prompt="hello")
    await provider.generate(request)

    assert request.data["_actual_model"] == "actual/model"
    assert request.data["_usage"] == {
        "prompt_tokens": 123456,
        "completion_tokens": 789,
        "cost_usd": 0.0042,
    }


@pytest.mark.asyncio
async def test_openrouter_missing_usage_stays_none_not_zero(monkeypatch):
    provider = _adapter(monkeypatch, {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": "not-a-number", "completion_tokens": True},
    })
    request = AIRequest(capability="chat", prompt="hello")
    await provider.generate(request)

    assert request.data["_usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
    }


# ==================== redaction stays safe for telemetry ==================== #


def test_six_digit_token_counts_survive_redaction_but_secrets_do_not():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    telemetry_logger = logging.getLogger("app.ai.telemetry.test")
    telemetry_logger.handlers = [handler]
    telemetry_logger.propagate = False
    telemetry_logger.setLevel(logging.INFO)

    try:
        telemetry_logger.info(json.dumps({
            "tokens_prompt": 123456,
            "tokens_completion": 654321,
            "api_key": "SUPER_SECRET_VALUE",
        }))
    finally:
        telemetry_logger.handlers = []
        telemetry_logger.propagate = True

    line = stream.getvalue()
    assert '"tokens_prompt": 123456' in line
    assert '"tokens_completion": 654321' in line
    assert "SUPER_SECRET_VALUE" not in line
    assert "[REDACTED]" in line
