"""Post-merge privacy regressions found in the PR #16 audit.

Each test here fails on the merged code and passes only with the fix:

* the retention cutoff used an ISO ``T`` while SQLite ``CURRENT_TIMESTAMP``
  writes a space, deleting activity rows up to 24 hours before the window;
* a failed verification read was reported as a clean deletion;
* an unexpected exception during erasure left the transaction uncommitted;
* an unreadable storage-key list deleted the rows and orphaned the files;
* the Telegram success text claimed "all data" even when stored files survived.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-post-merge-audit")
os.environ["OPENROUTER_API_KEY"] = ""

import pytest

from app.application.privacy import DeletionResult, PrivacyService
from app.database.connection import (
    DatabaseManager,
    _CORE_INDEXES_DDL,
    _CORE_TABLES_DDL,
)
from app.database.migrations import apply_migrations
from app.database.repositories import Database
from app.telegram import privacy_handlers as P

OWNER = 9401
OTHER = 9402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "post_merge_privacy.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    for statement in _CORE_TABLES_DDL:
        cursor.execute(statement)
    for statement in _CORE_INDEXES_DDL:
        cursor.execute(statement)
    apply_migrations(conn)
    conn.commit()
    conn.close()
    return path


def _database(path: str) -> Database:
    manager = DatabaseManager()
    manager.db_path = path
    return Database(manager)


def _seed(path: str) -> None:
    database = _database(path)
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        database.create_user(OWNER, "owner", "Owner", "")
        database.create_user(OTHER, "other", "Other", "")
        database.plans.create(
            OWNER, today, [{"task": "A"}], [], "20:00", "10"
        )
        database.plans.create(
            OTHER, today, [{"task": "Z"}], [], "20:00", "10"
        )
    finally:
        database._manager._close_connection()


def _rows(path: str, sql: str, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _boom(*_args, **_kwargs):
    raise sqlite3.OperationalError("simulated read failure")


# ---------------------------------------------------------------------------
# Retention cutoff
# ---------------------------------------------------------------------------


def test_activity_purge_matches_the_current_timestamp_format(db_path):
    """Rows on the cutoff day but after the cutoff must survive."""
    reference = datetime(2026, 4, 1, 3, 10, 0)
    cutoff = reference - timedelta(days=90)  # 2026-01-01 03:10:00
    stamps = {
        "just_older": cutoff - timedelta(seconds=1),
        "at_cutoff": cutoff,
        "just_newer": cutoff + timedelta(seconds=1),
        "later_same_day": cutoff.replace(hour=20, minute=0, second=0),
    }
    conn = sqlite3.connect(db_path)
    try:
        for action, stamp in stamps.items():
            conn.execute(
                "INSERT INTO activity_log (user_id, action, details, timestamp) "
                "VALUES (?, ?, '', ?)",
                (OWNER, action, stamp.strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()
    finally:
        conn.close()

    database = _database(db_path)
    try:
        deleted = database.privacy.purge_expired_logs(90, 0, now=reference)
    finally:
        database._manager._close_connection()

    assert deleted == {"activity_log": 1, "coach_logs": 0}
    remaining = {
        action for (action,) in _rows(db_path, "SELECT action FROM activity_log")
    }
    assert remaining == {"at_cutoff", "just_newer", "later_same_day"}


# ---------------------------------------------------------------------------
# Verification failure is not success
# ---------------------------------------------------------------------------


def test_counts_returns_none_when_the_read_fails(db_path, monkeypatch):
    database = _database(db_path)
    try:
        monkeypatch.setattr(database.privacy, "_count", _boom)
        assert database.privacy.counts(OWNER) is None
    finally:
        database._manager._close_connection()


def test_delete_account_is_not_verified_when_the_check_read_fails(
    db_path, monkeypatch
):
    _seed(db_path)
    database = _database(db_path)
    service = PrivacyService(db=database)
    try:
        monkeypatch.setattr(database.privacy, "_count", _boom)
        result = service.delete_account(OWNER)
    finally:
        database._manager._close_connection()

    assert result is not None, "the committed erasure should still be reported"
    assert result.verified is False
    assert result.residual is None, "an unreadable check is not an empty result"
    # The erasure itself did commit; only the proof is missing.
    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,)) == []
    assert _rows(
        db_path, "SELECT * FROM users WHERE user_id = ?", (OTHER,)
    ), "the other user must not be touched"


# ---------------------------------------------------------------------------
# Unexpected exception rolls the transaction back
# ---------------------------------------------------------------------------


def test_delete_all_rolls_back_on_an_unexpected_exception(db_path, monkeypatch):
    _seed(db_path)
    database = _database(db_path)

    class _FlakyCursor:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.startswith("DELETE FROM goals"):
                raise RuntimeError("simulated cursor failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _FlakyConnection:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return _FlakyCursor(self._real.cursor())

        def __getattr__(self, name):
            return getattr(self._real, name)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                database.privacy,
                "_connection",
                lambda: _FlakyConnection(database._manager._get_connection()),
            )
            assert database.privacy.delete_all(OWNER) is None

        # Force a commit on the same thread-local connection. Before the fix the
        # rollback never ran, so this commit would persist the partial erasure.
        database.log_activity(OWNER, "commit_probe", "probe")
    finally:
        database._manager._close_connection()

    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,))
    assert _rows(
        db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OWNER,)
    ), "a partially deleted account was committed"
    assert _rows(db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OTHER,))


# ---------------------------------------------------------------------------
# Storage-key read failure aborts before rows are destroyed
# ---------------------------------------------------------------------------


def test_storage_keys_returns_none_when_the_read_fails(db_path, monkeypatch):
    database = _database(db_path)
    try:
        monkeypatch.setattr(database.privacy, "_connection", _boom)
        assert database.privacy.storage_keys(OWNER) is None
    finally:
        database._manager._close_connection()


def test_delete_account_aborts_when_storage_keys_cannot_be_read(
    db_path, monkeypatch
):
    _seed(db_path)
    database = _database(db_path)

    class _Storage:
        def delete(self, _key):
            return True

    service = PrivacyService(db=database, knowledge_storage=_Storage())
    try:
        monkeypatch.setattr(
            database.privacy, "storage_keys", lambda _user_id: None
        )
        assert service.delete_account(OWNER) is None
    finally:
        database._manager._close_connection()

    # Nothing may be deleted: without the keys the files could never be found.
    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,))
    assert _rows(db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OWNER,))


# ---------------------------------------------------------------------------
# Retention indexes
# ---------------------------------------------------------------------------


def test_retention_indexes_exist(db_path):
    names = {
        name
        for (name,) in _rows(
            db_path,
            "SELECT name FROM sqlite_master WHERE type = 'index'",
        )
    }
    assert {
        "idx_activity_log_timestamp",
        "idx_activity_log_user",
        "idx_coach_logs_date",
        "idx_coach_logs_user_date",
    } <= names


# ---------------------------------------------------------------------------
# Telegram honesty about files and unverified deletions
# ---------------------------------------------------------------------------


class _Message:
    def __init__(self):
        self.text = ""
        self.reply_text = AsyncMock(return_value=None)
        self.reply_document = AsyncMock(return_value=None)


class _Query:
    def __init__(self, data):
        self.data = data
        self.message = _Message()
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _Update:
    def __init__(self, user_id=OWNER, data=None):
        self.effective_user = SimpleNamespace(id=user_id)
        self.callback_query = _Query(data) if data is not None else None
        self.message = _Message() if data is None else None


class _Context:
    def __init__(self, container, pending_token=None, user_id=OWNER):
        self.bot_data = {"container": container}
        self.user_data = {}
        if pending_token is not None:
            self.user_data[P._PENDING_KEY] = {
                "token": pending_token,
                "user_id": user_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            }


def _container(result):
    return SimpleNamespace(
        privacy=SimpleNamespace(delete_account=lambda _user_id: result)
    )


def _rendered(update) -> str:
    return "\n".join(
        call.args[0] if call.args else ""
        for call in update.callback_query.edit_message_text.call_args_list
    )


@pytest.mark.asyncio
async def test_confirmation_does_not_claim_all_files_were_deleted():
    result = DeletionResult(
        deleted={"goals": 1},
        files_removed=1,
        files_failed=2,
        verified=True,
        residual={},
    )
    token = "file-failure-token"
    context = _Context(_container(result), pending_token=token)
    update = _Update(data=f"privacy_delete_confirm:{token}")

    await P.delete_confirm_callback(update, context)

    text = _rendered(update)
    assert "تعذّر حذف" in text and "2" in text
    assert "✅" not in text
    assert P._PENDING_KEY not in context.user_data


@pytest.mark.asyncio
async def test_unverified_read_is_reported_as_unverified_not_as_residual():
    result = DeletionResult(
        deleted={"goals": 1},
        files_removed=0,
        files_failed=0,
        verified=False,
        residual=None,
    )
    token = "unverified-token"
    context = _Context(_container(result), pending_token=token)
    update = _Update(data=f"privacy_delete_confirm:{token}")

    await P.delete_confirm_callback(update, context)

    text = _rendered(update)
    assert "تعذّر التحقق" in text
    assert "✅" not in text
    assert P._PENDING_KEY not in context.user_data


@pytest.mark.asyncio
async def test_delete_offer_refuses_when_the_summary_cannot_be_read():
    container = SimpleNamespace(privacy=SimpleNamespace(summary=lambda _uid: None))
    context = _Context(container)
    update = _Update()

    await P.offer_deletion(update, context, container)

    text = update.message.reply_text.await_args.args[0]
    assert "تعذر قراءة" in text
    assert P._PENDING_KEY not in context.user_data
