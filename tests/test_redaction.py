import logging
import sys

from redaction import redact, RedactingFormatter

_FAKE_TOKEN = "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken"


def test_redact_telegram_token_literal():
    out = redact(f"token={_FAKE_TOKEN}")
    assert "DummyToken" not in out


def test_redact_telegram_token_in_url():
    out = redact(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getMe")
    assert "DummyToken" not in out


def test_redact_api_key_value():
    out = redact("api_key=SUPER_SECRET_VALUE used here")
    assert "SUPER_SECRET_VALUE" not in out


def test_redact_keeps_benign_text():
    out = redact("FOREIGN KEY constraint failed")
    assert "FOREIGN" in out


def test_formatter_redacts_record():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1,
        "invalid token: %s", (_FAKE_TOKEN,), None,
    )
    formatted = RedactingFormatter(fmt="%(message)s").format(record)
    assert "DummyToken" not in formatted


def test_formatter_redacts_exception_traceback():
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "boom", (), None)
    try:
        raise ValueError(f"The token `{_FAKE_TOKEN}` was rejected by the server.")
    except ValueError:
        record.exc_info = sys.exc_info()
    formatted = RedactingFormatter(fmt="%(message)s").format(record)
    assert "DummyToken" not in formatted