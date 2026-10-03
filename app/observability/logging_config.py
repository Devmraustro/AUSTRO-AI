"""AUSTRO AI - Logging configuration.

Configures the root logger with UTF-8 console + file handlers and a
redacting formatter so secrets never land in `logs/bot.log`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from app.config.settings import Settings
from app.security.redaction import RedactingFormatter

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def _resolve_level(configured: Optional[str]) -> int:
    """Map AUSTRO_LOG_LEVEL to a logging level, defaulting to INFO."""
    if not configured:
        return logging.INFO
    return _LEVELS.get(str(configured).strip().upper(), logging.INFO)


def setup_logging(settings_: Optional[Settings]) -> None:
    """Idempotently configure root logging for the application."""
    logs_path = (settings_.logs_path if settings_ else None) or str(
        Path(__file__).resolve().parent.parent.parent / "logs"
    )
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # The audit found AUSTRO_LOG_LEVEL was loaded into settings but ignored:
    # the root logger was hardcoded to INFO, so an operator asking for WARNING
    # (e.g. to keep PII out of logs) still got INFO records.
    level = _resolve_level(getattr(settings_, "log_level", None))

    formatter = RedactingFormatter(fmt=_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    # Idempotent: repeated setup_logging() calls (tests, re-init) must not stack
    # duplicate handlers, which would double every line.
    wanted = {"stream", "file"}
    existing = {
        getattr(h, "_austro_kind", "")
        for h in root.handlers
        if getattr(h, "_austro_kind", "") in wanted
    }
    if not existing.issuperset(wanted):
        for handler in list(root.handlers):
            if getattr(handler, "_austro_kind", "") in wanted:
                root.removeHandler(handler)
        stream = logging.StreamHandler(sys.stdout)
        stream._austro_kind = "stream"
        file_handler = logging.FileHandler(
            f"{logs_path}/bot.log", encoding="utf-8", errors="replace"
        )
        file_handler._austro_kind = "file"
        for handler in (stream, file_handler):
            handler.setFormatter(formatter)
            root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)