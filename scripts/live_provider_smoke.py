"""Live provider smoke check (manual, credential-only, read-only).

Validates the Telegram bot token with `getMe` (no message is ever sent) and
makes one minimal Gemini `generateContent` request. Credentials come only
from the environment; diagnostics are redacted and length-limited.

Extracted from the workflow heredoc so the retry/classification logic is
unit-testable with mocked HTTP (tests/test_live_provider_smoke.py). The
application default model lives in `app.config.settings.DEFAULT_GEMINI_MODEL`;
a consistency test keeps this module's fallback, the workflow input default,
and `.env.example` in agreement with it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_ATTEMPTS = 3
MAX_WAIT_SECONDS = 30
TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")


def safe_google_error(body: bytes, api_key: str) -> str:
    """Small redacted diagnostic; never includes headers or request data."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
        error = payload.get("error", {})
        status = str(error.get("status", "UNKNOWN"))
        message = str(error.get("message", "No provider message"))
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        message = re.sub(r"AIza[0-9A-Za-z_-]{10,}", "[REDACTED]", message)
        message = re.sub(r"https?://\S+", "[URL]", message)
        message = re.sub(r"[\r\n\t]+", " ", message)
        return f"status={status}; message={message[:180]}"
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return "provider error details unavailable"


def _retry_wait(attempt: int, retry_after: str | None) -> float:
    """Bounded wait: honor Retry-After (seconds) up to the strict cap."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), MAX_WAIT_SECONDS)
        except ValueError:
            pass  # HTTP-date form: fall back to exponential backoff
    return min(float(2 ** attempt), MAX_WAIT_SECONDS)


def check_telegram(token: str, opener=urllib.request.urlopen) -> tuple[bool, str]:
    if not token:
        return False, "Telegram getMe: FAIL (repository secret BOT_TOKEN is missing or empty)"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        headers={"Accept": "application/json"}, method="GET")
    try:
        with opener(request, timeout=15) as response:
            data = json.loads(response.read())
        if data.get("ok") is True and isinstance(data.get("result"), dict) \
                and data["result"].get("is_bot") is True:
            return True, "Telegram getMe: PASS (valid bot token; identity details suppressed)"
        return False, "Telegram getMe: FAIL (unexpected API response; body suppressed)"
    except urllib.error.HTTPError as exc:
        return False, f"Telegram getMe: FAIL (HTTP {exc.code}; response body suppressed)"
    except Exception as exc:  # noqa: BLE001
        return False, f"Telegram getMe: FAIL (network/client error: {type(exc).__name__}; details suppressed)"


def check_gemini(api_key: str, model: str, opener=urllib.request.urlopen,
                 sleeper=time.sleep) -> tuple[bool, str]:
    if not api_key:
        return False, "Gemini generateContent: FAIL (repository secret GEMINI_API_KEY is missing or empty)"
    if not _MODEL_RE.fullmatch(model or ""):
        return False, "Gemini generateContent: FAIL (invalid model identifier)"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{quote(model, safe='._-')}:generateContent")
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": "Reply with exactly OK."}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8},
    }).encode("utf-8")
    last_failure = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "x-goog-api-key": api_key})
        try:
            with opener(request, timeout=30) as response:
                data = json.loads(response.read())
            candidates = data.get("candidates")
            if isinstance(candidates, list) and candidates:
                return True, (f"Gemini generateContent: PASS (model={model}; "
                              f"output suppressed; attempt={attempt})")
            return False, "Gemini generateContent: FAIL (no candidate returned)"
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_HTTP and attempt < MAX_ATTEMPTS:
                wait = _retry_wait(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                print(f"Gemini generateContent: transient HTTP {exc.code}; "
                      f"retry {attempt + 1}/{MAX_ATTEMPTS} after {wait:g}s")
                sleeper(wait)
                continue
            diagnostic = safe_google_error(exc.read(4096), api_key)
            return False, f"Gemini generateContent: FAIL (HTTP {exc.code}; {diagnostic})"
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if attempt < MAX_ATTEMPTS:
                wait = _retry_wait(attempt, None)
                print(f"Gemini generateContent: transient {type(reason).__name__}; "
                      f"retry {attempt + 1}/{MAX_ATTEMPTS} after {wait:g}s")
                sleeper(wait)
                continue
            return False, f"Gemini generateContent: FAIL (network/client error: {type(reason).__name__})"
        except Exception as exc:  # noqa: BLE001
            return False, f"Gemini generateContent: FAIL (client error: {type(exc).__name__})"
    return False, f"Gemini generateContent: FAIL ({last_failure or 'retry budget exhausted'})"


def main() -> int:
    failures = []
    ok, line = check_telegram(os.environ.get("BOT_TOKEN", "").strip())
    print(line)
    if not ok:
        failures.append("Telegram")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    ok, line = check_gemini(os.environ.get("GEMINI_API_KEY", "").strip(), model)
    print(line)
    if not ok:
        failures.append("Gemini")
    if failures:
        print("LIVE PROVIDER SMOKE: FAIL (" + ", ".join(failures) + ")")
        return 1
    print("LIVE PROVIDER SMOKE: PASS (both providers reachable; no Telegram message sent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
