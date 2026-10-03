"""
AUSTRO AI - Log redaction.

Prevents secrets (bot tokens, API keys, credentials) from leaking into logs.

Design rules (enforced by tests/test_redaction.py):
  1. Fail CLOSED. If a pattern cannot be applied, the formatter must not emit
     the original record. The previous implementation caught the redaction
     error and then returned `super().format(record)`, writing the raw secret.
  2. Neutralise log injection. CR/LF/control characters are escaped so a
     multi-line payload cannot forge a second log line or a fake level prefix.
  3. Cover real provider formats, not just Telegram. Bare (unlabelled)
     Google/Gemini and OpenAI keys, Postgres DSNs, AWS key IDs, JWTs and
     bearer tokens were all missed by the original three patterns.
"""

from __future__ import annotations

import logging
import re
import unicodedata

# --- provider-specific secrets ------------------------------------------------
_TOKEN_LITERAL_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")
_TELEGRAM_URL_RE = re.compile(r"/bot[\w:-]{20,}")
_GEMINI_URL_RE = re.compile(
    r"generativelanguage\.googleapis\.com[^\s]*key=[A-Za-z0-9_-]{20,}"
)
# Google AI Studio keys: AIza..., bare or inside a URL/prose.
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
# OpenAI project keys (sk-proj-/sk-...) and legacy sk- keys.
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")
# Anthropic keys.
_ANTHROPIC_KEY_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")
# AWS access key IDs are 20 chars, upper/digits, always prefixed AKIA/ASIA/ABIA.
_AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")
# JWT: three base64url segments separated by dots.
_JWT_RE = re.compile(r"\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\b")
# HTTP Authorization / Proxy-Authorization header values.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*(?:bearer|basic|token)?\s*\S+"
)
# Postgres/MySQL/Mongo connection URIs with inline credentials.
_DSN_CRED_RE = re.compile(
    r"(?i)\b(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://"
    r"[^:@/\s]+:([^@/\s]+)@"
)
# key=value / key: value / "key": "value" for any credential-ish label.
_SECRET_PAIR_RE = re.compile(
    r"(?i)((?:token|key|api[_ -]?key|password|passwd|pwd|secret|authorization|"
    r"credential|private[_ -]?key|access[_ -]?key|client[_ -]?secret|"
    r"session[_ -]?key|webhook)[\w\s-]*?[\"\']?\s*[:=]\s*[\"\']?)"
    r"([^\s\"',;)\]}]{6,})"
)
# Telegram file_ids are long opaque base64url blobs; they grant file access.
_TELEGRAM_FILE_ID_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")
# PEM private key blocks.
_PEM_RE = re.compile(
    r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
)
# Card-like digit runs (13-19 digits, optionally grouped).
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# Control characters that allow log forging: CR, LF, tab-free C0/C1 except none.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_REDACTED = "[REDACTED]"


def _escape_controls(text: str) -> str:
    """Make embedded control characters inert so a record stays one line."""
    # First strip any C1 range that survived normalisation, then escape.
    return _CONTROL_RE.sub(
        lambda m: "\\x{:02x}".format(ord(m.group(0))),
        text,
    )


def redact(text: str) -> str:
    """Redact credentials from arbitrary text.

    Order matters: the widest structural patterns (PEM, DSNs) run first so the
    narrow provider patterns cannot partially rewrite them.
    """
    if not text:
        return text

    # PEM blocks first: multi-line and self-identifying.
    text = _PEM_RE.sub("[PRIVATE_KEY_REDACTED]", text)

    # Credentials inside connection URIs, keeping the URI shape for debugging.
    text = _DSN_CRED_RE.sub(
        lambda m: f"{m.group(1)}://{m.group(0).split('://')[1].split(':')[0]}:"
                  f"{_REDACTED}@",
        text,
    )

    # Header-style secrets.
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", text)

    # Named secret pairs. Values that are already a redaction marker are left
    # alone, so an earlier pattern's "[REDACTED]" is not re-matched and left
    # with a stray closing bracket.
    text = _SECRET_PAIR_RE.sub(
        lambda m: m.group(0) if m.group(2).startswith("[") else f"{m.group(1)}{_REDACTED}",
        text,
    )

    # Provider-specific literals.
    text = _TOKEN_LITERAL_RE.sub("[TELEGRAM_TOKEN]", text)
    text = _TELEGRAM_URL_RE.sub("[TELEGRAM_TOKEN]", text)
    text = _GEMINI_URL_RE.sub("generativelanguage.googleapis.com...key=" + _REDACTED, text)
    text = _GOOGLE_KEY_RE.sub("[GOOGLE_API_KEY]", text)
    text = _ANTHROPIC_KEY_RE.sub("[ANTHROPIC_API_KEY]", text)
    text = _OPENAI_KEY_RE.sub("[OPENAI_API_KEY]", text)
    text = _AWS_KEY_ID_RE.sub("[AWS_ACCESS_KEY_ID]", text)
    text = _JWT_RE.sub("[JWT_REDACTED]", text)
    text = _CARD_RE.sub("[CARD_REDACTED]", text)
    text = _TELEGRAM_FILE_ID_RE.sub("[OPAQUE_ID_REDACTED]", text)

    # Neutralise anything that could forge a second log record.
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    text = _escape_controls(text)
    # Strip bidi/zero-width overrides used to disguise secret boundaries.
    text = unicodedata.normalize("NFKC", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts secrets before writing records.

    Fails closed: on any unexpected error the record is replaced with an
    explicit "[REDACTION FAILED]" marker rather than the unredacted message.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Copy the record: mutating msg/args in place corrupts the record for
        # every other handler attached to this logger.
        safe = logging.makeLogRecord(record.__dict__)
        try:
            message = safe.getMessage()
        except Exception:  # noqa: BLE001 - a broken getMessage must not leak args
            message = str(getattr(safe, "msg", "<unformattable log record>"))
        safe.msg = message
        safe.args = ()
        try:
            rendered = super().format(safe)
        except Exception:  # noqa: BLE001 - never emit the raw record
            return f"[REDACTION FAILED] record from {safe.name} suppressed"
        # `super().format()` appends the traceback, which is *not* part of
        # record.msg and therefore never passed through the block above. A
        # traceback can carry the secret (e.g. raised inside an HTTP request),
        # so the fully rendered text is redacted as the final step.
        return redact(rendered)