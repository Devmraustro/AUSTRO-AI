"""
AUSTRO AI - Log redaction.

Prevents secrets (bot tokens, API keys) from leaking into logs. Verbatim
move of the original top-level `redaction` module.
"""

from __future__ import annotations

import logging
import re

_TOKEN_LITERAL_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")
_TELEGRAM_URL_RE = re.compile(r"/bot[\w:-]{20,}")
_GEMINI_URL_RE = re.compile(r"generativelanguage\.googleapis\.com[^\s]*key=[A-Za-z0-9_-]{20,}")
_SECRET_PAIR_RE = re.compile(
    r"(?i)((?:token|key|api[_ -]?key|password|secret|authorization)[^\w]*)([\w.:/_+=-]{6,})"
)


def redact(text: str) -> str:
    """Redact bearer tokens, Telegram tokens and secret key values."""
    text = _TOKEN_LITERAL_RE.sub("[TELEGRAM_TOKEN]", text or "")
    text = _TELEGRAM_URL_RE.sub("[TELEGRAM_TOKEN]", text)
    text = _GEMINI_URL_RE.sub("generativelanguage.googleapis.com...key=[REDACTED]", text)
    text = _SECRET_PAIR_RE.sub(r"\1[REDACTED]", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts secrets before writing records."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - redaction must never break logging
            pass
        try:
            return redact(super().format(record))
        except Exception:  # noqa: BLE001
            return super().format(record)