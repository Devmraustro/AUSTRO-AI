"""Offline smoke test: verifies the app builds and core subsystems work without a network.

Runs in CI before any real bot start. Uses a dummy token and a temp database.
Exits non-zero on any failure.
"""

import os
import sys
import tempfile

_FAKE_TOKEN = "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken"

os.environ.setdefault("BOT_TOKEN", _FAKE_TOKEN)
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

import main as app_module  # noqa: E402
import redaction  # noqa: E402
from database import db  # noqa: E402


def run_smoke() -> int:
    app = app_module.build_application()
    assert len(app.handlers) >= 1, "no handlers registered"
    assert app.post_init is not None, "post_init missing"
    assert app.post_stop is not None, "post_stop missing"

    assert db.get_all_user_ids() == []
    assert db.create_user(1, "smoke_user", "Smoke")

    rid = db.create_reminder(1, "custom", "Test", "Reminder message", "2026-01-01T08:00:00", False, None)
    assert rid is not None, "create_reminder failed"
    assert len(db.get_all_active_reminders()) == 1, "get_all_active_reminders failed"

    secret = f"token={_FAKE_TOKEN} and api_key=SUPER_SECRET_VALUE"
    out = redaction.redact(secret)
    assert "DummyToken" not in out, "token not redacted"
    assert "SUPER_SECRET_VALUE" not in out, "key not redacted"

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_smoke())