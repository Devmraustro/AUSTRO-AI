"""Deterministic fault-injection tests for Telegram polling reliability.

These tests mock ``build_application`` and ``Application.run_polling`` so no
network, DNS, or Telegram API calls are made. They verify that the application
processes polling failures cleanly (logs and stops) instead of crashing, and
that command registration is wired into startup.

Live recovery from a real network outage or Telegram API outage is handled by
python-telegram-bot's internal retry logic plus external process supervision
(systemd / Docker restart policy); it is NOT re-implemented here as a
self-restart loop, which would conflict with the hosting platform.

Verified against PTB 22.8 source:

* ``Application.run_polling`` raises only when the app has no Updater
  (``RuntimeError``) or when bootstrapping fails. ``bootstrap_retries``
  defaults to ``0``, so bootstrap errors (network timeout, ``RetryAfter``,
  invalid token, generic ``TelegramError``) propagate out of ``run_polling``
  on the first failed attempt.
* During the polling loop itself, PTB handles ``get_updates`` errors
  internally through its ``error_callback`` (registered error handlers); they
  do **not** propagate out of ``run_polling``. ``RetryAfter`` and ``TimedOut``
  are retried internally by PTB's ``network_retry_loop`` with backoff.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from telegram import Update
from telegram.error import (
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

import app.telegram.main as M


def _make_app(run_polling_side_effect=None):
    app = MagicMock()
    app.run_polling = MagicMock(side_effect=run_polling_side_effect)
    return app


def test_main_handles_polling_network_error(monkeypatch):
    """A transient network error during bootstrap is logged and stops cleanly.

    PTB retries transient errors internally during the polling loop, so only
    bootstrap-time network errors reach ``run_polling`` (with
    ``bootstrap_retries=0``). The application must log and stop rather than
    crash with a traceback.
    """
    app = _make_app(NetworkError("connection lost"))
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()  # must not raise

    app.run_polling.assert_called_once_with(allowed_updates=Update.ALL_TYPES)


def test_main_handles_polling_timeout(monkeypatch):
    """A polling timeout is logged and stops cleanly."""
    app = _make_app(TimedOut("read timed out"))
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()  # must not raise

    app.run_polling.assert_called_once_with(allowed_updates=Update.ALL_TYPES)


def test_main_handles_polling_telegram_error(monkeypatch):
    """A generic Telegram API error stops cleanly."""
    app = _make_app(TelegramError("forbidden"))
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()  # must not raise

    app.run_polling.assert_called_once_with(allowed_updates=Update.ALL_TYPES)


def test_main_handles_retry_after_clean_stop(monkeypatch):
    """HTTP 429 (``RetryAfter``) during bootstrap stops cleanly.

    PTB respects ``RetryAfter`` internally during the polling loop; during
    bootstrap (``bootstrap_retries=0``) it raises immediately, so the app
    stops cleanly rather than entering a retry storm.
    """
    app = _make_app(RetryAfter(retry_after=30))
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()  # must not raise

    app.run_polling.assert_called_once_with(allowed_updates=Update.ALL_TYPES)


def test_main_handles_invalid_token_permanent_failure(monkeypatch):
    """A permanent authentication failure stops cleanly instead of retrying.

    ``InvalidToken`` is raised immediately by PTB's bootstrap retry loop and
    must not be retried by the application.
    """
    app = _make_app(InvalidToken("invalid token"))
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()  # must not raise

    app.run_polling.assert_called_once_with(allowed_updates=Update.ALL_TYPES)


def test_build_application_wires_command_registration():
    """The built application registers native commands on post_init."""
    app = M.build_application()
    assert app.post_init is M._post_init


def test_run_polling_invoked_exactly_once(monkeypatch):
    """Polling is started exactly once with all update types.

    The application must not implement its own retry loop around polling;
    exactly one polling consumer must exist for the bot token.
    """
    app = _make_app()
    monkeypatch.setattr(M, "build_application", lambda: app)

    M.main()

    app.run_polling.assert_called_once()
    call_kwargs = app.run_polling.call_args.kwargs
    assert call_kwargs.get("allowed_updates") == Update.ALL_TYPES
