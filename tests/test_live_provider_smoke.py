"""Deterministic tests for the live-provider smoke script (mocked HTTP).

No real network or API quota is consumed. Covers the required matrix:
deterministic failures are never retried, transient failures are retried
with bounded backoff honoring Retry-After, the retry budget is finite,
diagnostics never leak credentials, Gemini 3 payload compatibility, and
model-default consistency across configuration surfaces.
"""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from scripts import live_provider_smoke as smoke


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _http_error(code, body=b"{}", retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x", code, "err", headers, io.BytesIO(body))


class _Opener:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.requests = []

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok_candidate():
    return _Resp(json.dumps({"candidates": [{"content": {}}]}).encode())


def _no_sleep(_seconds):
    return None


def _gemini(opener, model="gemini-3.8-flash"):
    return smoke.check_gemini("SECRETKEY", model, opener=opener, sleeper=_no_sleep)


def test_valid_response_with_candidate_passes():
    opener = _Opener([_ok_candidate()])
    ok, line = _gemini(opener)
    assert ok and "PASS" in line and opener.calls == 1


def test_gemini3_payload_omits_unsupported_sampling_parameters():
    opener = _Opener([_ok_candidate()])
    ok, _ = _gemini(opener)
    assert ok
    payload = json.loads(opener.requests[0].data)
    assert payload["generationConfig"] == {"maxOutputTokens": 8}
    assert not ({"temperature", "topP", "topK", "candidateCount", "thinkingBudget"}
                & set(payload["generationConfig"]))


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_deterministic_http_errors_fail_without_retries(code):
    opener = _Opener([_http_error(code, b'{"error": {"status": "X", "message": "m SECRETKEY"}}')])
    ok, line = _gemini(opener)
    assert not ok and opener.calls == 1
    assert "SECRETKEY" not in line


def test_429_retries_with_retry_after_then_succeeds():
    waits = []
    opener = _Opener([_http_error(429, retry_after="7"), _ok_candidate()])
    ok, line = smoke.check_gemini("SECRETKEY", "gemini-3.8-flash",
                                  opener=opener, sleeper=waits.append)
    assert ok and opener.calls == 2 and waits == [7.0]


def test_retry_after_is_capped():
    waits = []
    opener = _Opener([_http_error(503, retry_after="3600"), _ok_candidate()])
    ok, _ = smoke.check_gemini("SECRETKEY", "gemini-3.8-flash",
                               opener=opener, sleeper=waits.append)
    assert ok and waits == [smoke.MAX_WAIT_SECONDS]


@pytest.mark.parametrize("code", [500, 503])
def test_transient_http_exhausts_budget_and_fails(code):
    opener = _Opener([_http_error(code)] * smoke.MAX_ATTEMPTS)
    ok, _ = _gemini(opener)
    assert not ok and opener.calls == smoke.MAX_ATTEMPTS


def test_timeout_retries_then_succeeds():
    opener = _Opener([TimeoutError("t"), _ok_candidate()])
    ok, _ = _gemini(opener)
    assert ok and opener.calls == 2


def test_network_exhaustion_fails():
    opener = _Opener([urllib.error.URLError("down")] * smoke.MAX_ATTEMPTS)
    ok, _ = _gemini(opener)
    assert not ok and opener.calls == smoke.MAX_ATTEMPTS


def test_empty_and_malformed_responses_fail_without_retry():
    opener = _Opener([_Resp(b'{"candidates": []}')])
    ok, _ = _gemini(opener)
    assert not ok and opener.calls == 1
    opener = _Opener([_Resp(b"not-json")])
    ok, _ = _gemini(opener)
    assert not ok and opener.calls == 1


def test_missing_key_and_invalid_model_fail_before_any_request():
    opener = _Opener([])
    ok, _ = smoke.check_gemini("", "gemini-3.8-flash", opener=opener, sleeper=_no_sleep)
    assert not ok and opener.calls == 0
    ok, _ = smoke.check_gemini("SECRETKEY", "bad model!", opener=opener, sleeper=_no_sleep)
    assert not ok and opener.calls == 0


def test_custom_model_override_is_the_one_sent():
    opener = _Opener([_ok_candidate()])
    ok, _ = _gemini(opener, model="gemini-3.5-flash-lite")
    assert ok and "gemini-3.5-flash-lite" in opener.requests[0].full_url


def test_telegram_getme_only_and_missing_token():
    opener = _Opener([_Resp(json.dumps({"ok": True, "result": {"is_bot": True}}).encode())])
    ok, _ = smoke.check_telegram("t", opener=opener)
    assert ok and opener.requests[0].method == "GET" and "getMe" in opener.requests[0].full_url
    ok, _ = smoke.check_telegram("", opener=_Opener([]))
    assert not ok


def test_model_default_consistent_across_config_surfaces():
    root = Path(__file__).resolve().parent.parent
    from app.config.settings import DEFAULT_GEMINI_MODEL
    assert DEFAULT_GEMINI_MODEL == smoke.DEFAULT_MODEL
    workflow = (root / ".github/workflows/live-provider-smoke.yml").read_text()
    assert f'default: "{DEFAULT_GEMINI_MODEL}"' in workflow
    env_example = (root / ".env.example").read_text()
    assert f"GEMINI_MODEL={DEFAULT_GEMINI_MODEL}" in env_example


def test_redaction_removes_urls_newlines_and_api_key():
    body = json.dumps({
        "error": {
            "status": "INVALID_ARGUMENT",
            "message": "bad key AIzaSyD-1234567890abcdef at https://x.example/y\nsecret SECRETKEY",
        }
    }).encode()
    line = smoke.safe_google_error(body, "SECRETKEY")
    assert "SECRETKEY" not in line
    assert "AIzaSyD-1234567890abcdef" not in line
    assert "https://" not in line and "\n" not in line
    assert "[URL]" in line and "[REDACTED]" in line
    assert len(line) <= len("status=INVALID_ARGUMENT; message=") + 180
    assert smoke.safe_google_error(b"\xff not json", "k") == "provider error details unavailable"


def test_retry_after_http_date_supported_and_capped():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    soon = (now + timedelta(seconds=9)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert smoke._retry_wait(1, soon, now=now) == 9.0
    far = (now + timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert smoke._retry_wait(1, far, now=now) == smoke.MAX_WAIT_SECONDS
    past = (now - timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert smoke._retry_wait(1, past, now=now) == 0.0


def test_unparseable_retry_after_falls_back_to_bounded_backoff():
    assert smoke._retry_wait(2, "soon-ish") == 4.0
    assert smoke._retry_wait(10, None) == smoke.MAX_WAIT_SECONDS


def test_retry_budget_never_exceeds_configured_limit():
    waits = []
    opener = _Opener([_http_error(503, retry_after="30")] * smoke.MAX_ATTEMPTS)
    ok, _ = smoke.check_gemini("SECRETKEY", "gemini-3.8-flash", opener=opener, sleeper=waits.append)
    assert not ok and opener.calls == smoke.MAX_ATTEMPTS == 3
    assert all(w <= smoke.MAX_WAIT_SECONDS for w in waits) and len(waits) == 2
