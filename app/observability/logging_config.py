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

    formatter = RedactingFormatter(fmt=_FORMAT)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                f"{logs_path}/bot.log", encoding="utf-8", errors="replace"
            ),
        ],
    )
    for handler in logging.root.handlers:
        handler.setFormatter(formatter)