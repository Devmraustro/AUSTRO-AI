"""Coach-log persistence contracts (A3).

The writer is exercised through the real `coach_analysis` handler against the
per-test SQLite database from `tests/conftest.py`. The AI gateway is stubbed at
the service boundary, so no provider is called.

The 90-day purge contract is intentionally not tested here on this base:
`PrivacyRepository` (and its `purge_expired_logs` helper) exists only in the
unmerged PR #15 branch. Adding a local DELETE here would duplicate that
helper's SQL, which the task explicitly forbids.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["OPENROUTER_API_KEY"] = ""

from app.database.repositories import CoachLogRepository  # noqa: E402
from app.telegram import handlers as H  # noqa: E402


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _FakeQuery:
    def __init__(self):
        self.data = "coach_analysis"
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUpdate:
    def __init__(self, user_id: int = 7):
        self.effective_user = _FakeUser(user_id)
        self.callback_query = _FakeQuery()
        self.message = None


class _FakeContext:
    def __init__(self, services):
        self.bot_data = {"container": services}
        self.user_data = {}


def _services(db, analysis: str = "تحليل الأداء"):
    return SimpleNamespace(
        db=db,
        dashboard=SimpleNamespace(stats=Mock(return_value={
            "total_goals": 2,
            "completed_goals": 1,
            "total_habits": 3,
            "total_streaks": 4,
            "weekly_study_hours": 5,
            "weekly_tasks": 6,
            "total_reviews": 7,
        })),
        coaching=SimpleNamespace(analyze=AsyncMock(return_value=analysis)),
    )


def _run(monkeypatch, services, user_id: int = 7):
    monkeypatch.setattr(H, "guard_ai_action", AsyncMock(return_value=True))
    update = _FakeUpdate(user_id)
    asyncio.run(H.coach_analysis(update, _FakeContext(services)))
    return update


def _rows(db):
    return db._get_connection().execute(
        "SELECT user_id, date, advice_given FROM coach_logs"
    ).fetchall()


def _seed_user(db, user_id: int = 7) -> None:
    """Create the caller row the FK requires (as `/start` does)."""
    assert db.create_user(user_id, "tester", "Test")


def test_database_facade_exposes_coach_logs(fresh_db):
    assert isinstance(fresh_db.coach_logs, CoachLogRepository)


def test_coach_analysis_writes_the_analysis(fresh_db, monkeypatch):
    _seed_user(fresh_db)
    analysis = "التحليل النهائي للأداء"
    update = _run(monkeypatch, _services(fresh_db, analysis))

    rows = _rows(fresh_db)
    assert len(rows) == 1
    assert rows[0]["user_id"] == 7
    assert rows[0]["advice_given"] == analysis
    update.callback_query.edit_message_text.assert_awaited()


def test_written_date_is_utc_yyyy_mm_dd(fresh_db, monkeypatch):
    _seed_user(fresh_db)
    _run(monkeypatch, _services(fresh_db))

    value = _rows(fresh_db)[0]["date"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
    assert value == datetime.utcnow().strftime("%Y-%m-%d")


def test_failing_insert_does_not_raise_or_drop_the_reply(monkeypatch):
    analysis = "تحليل رغم فشل الحفظ"
    broken_db = SimpleNamespace(
        coach_logs=SimpleNamespace(add=Mock(side_effect=RuntimeError("db down")))
    )
    update = _run(monkeypatch, _services(broken_db, analysis))

    texts = [
        call.args[0]
        for call in update.callback_query.edit_message_text.await_args_list
    ]
    assert texts[-1] == analysis
