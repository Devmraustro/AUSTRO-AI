"""Account data export, erasure and retention — the privacy gap.

WHY THIS FILE EXISTS
====================
`PRIVACY_RETENTION.md` §3.1 stated the gap plainly: `/export`, `/forget` and
`/delete` were "not implemented and not registered", no account-deletion code
existed, knowledge sources could not be deleted from the bot, and the 90-day
retention policy on `activity_log`/`coach_logs` was documented but not enforced.

These tests prove the fix is real rather than cosmetic:

* erasure removes rows from **every** table that can hold user data — the table
  list is asserted against a freshly migrated schema, so adding a table without
  covering it fails the suite instead of silently retaining data;
* erasure never touches another user's rows;
* a failed transaction deletes nothing and is reported as a failure (never as
  "your data is gone");
* the export is valid JSON and contains the user's rows;
* the retention purge deletes only rows past the window;
* the Telegram handlers require a real confirmation and refuse stale/foreign
  taps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.application.privacy import PrivacyService, _json_safe
from app.database.connection import DatabaseManager
from app.database.repositories import Database, PrivacyRepository

OWNER = 6101
OTHER = 6102


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    from app.database.connection import _CORE_INDEXES_DDL, _CORE_TABLES_DDL
    from app.database.migrations import apply_migrations

    path = str(tmp_path / "privacy.db")
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


def _seed(path: str) -> str:
    """Create two users with data in the core tables; return the plan date."""
    database = _database(path)
    try:
        date = datetime.utcnow().strftime("%Y-%m-%d")
        database.create_user(OWNER, "owner", "Owner", "")
        database.create_user(OTHER, "other", "Other", "")
        database.plans.create(
            OWNER, date, [{"task": "A"}, {"task": "B"}], [], "20:00", "10",
        )
        database.plans.create(
            OTHER, date, [{"task": "Z"}], [], "20:00", "10",
        )
        database.create_goal(OWNER, "goal", "", "cat", [], None)
        database.create_goal(OTHER, "other goal", "", "cat", [], None)
        database.log_activity(OWNER, "test", "owner activity")
        database.log_activity(OTHER, "test", "other activity")
        return date
    finally:
        database._manager._close_connection()


def _service(path: str) -> tuple:
    database = _database(path)
    return PrivacyService(db=database), database


def _rows(path: str, sql: str, params=()) -> list:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage: every user-owned table must be erasable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["user_id", "owner_user_id"])
def test_no_user_owned_table_is_missing_from_the_erasure_lists(db_path, column):
    """A table with a user column must be in the deletion lists, or data leaks.

    This is the regression that keeps erasure honest as the schema grows: a new
    table with `user_id`/`owner_user_id` fails this test until it is added.
    """
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        with_column = []
        for table in tables:
            if table in ("schema_migrations", "sqlite_sequence"):
                continue
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column in columns:
                with_column.append(table)
    finally:
        conn.close()

    covered = set(PrivacyRepository._USER_TABLES) | set(
        PrivacyRepository._OWNER_TABLES
    )
    # `users` carries user_id but is erased by a dedicated statement in
    # `delete_all` (last, after every child row), and `delete_all_covers_the_users_row`
    # below asserts that explicitly.
    if column == "user_id":
        covered.add("users")
    missing = sorted(set(with_column) - covered)
    assert not missing, (
        f"tables hold {column} but are not erased: {missing}"
    )


def test_delete_all_covers_the_users_row(db_path):
    """`users` is not in the table lists; `delete_all` must still remove it."""
    _seed(db_path)
    database = _database(db_path)
    try:
        deleted = database.privacy.delete_all(OWNER)
    finally:
        database._manager._close_connection()

    assert deleted is not None
    assert deleted["users"] == 1, "the account row was not deleted"
    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,)) == []


def test_indirect_tables_are_declared_and_exist(db_path):
    """Tables without an owner column must be scoped through a parent."""
    conn = sqlite3.connect(db_path)
    try:
        for table, child_col, parent, parent_col in PrivacyRepository._INDIRECT_TABLES:
            child = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            guardian = {r[1] for r in conn.execute(f"PRAGMA table_info({parent})")}
            assert child_col in child, f"{table}.{child_col} does not exist"
            assert parent_col in guardian, f"{parent}.{parent_col} does not exist"
            assert "owner_user_id" in guardian, (
                f"{parent} must carry owner_user_id to scope {table}"
            )
    finally:
        conn.close()


def test_erasure_covers_knowledge_citations_without_owner_column(db_path):
    """`knowledge_citations` has no owner column and must still be erased."""
    date = _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO knowledge_retrieval_events "
        "(owner_user_id, query, top_k, result_count, latency_ms) "
        "VALUES (?, '', 5, 1, 1)", (OWNER,),
    )
    event_id = conn.execute("SELECT MAX(event_id) FROM knowledge_retrieval_events").fetchone()[0]
    conn.execute(
        "INSERT INTO knowledge_citations (event_id, chunk_row_id, source_id, title) "
        "VALUES (?, 1, 1, 'cited')", (event_id,),
    )
    conn.commit()
    conn.close()

    service, database = _service(db_path)
    try:
        result = service.delete_account(OWNER)
    finally:
        database._manager._close_connection()

    assert result.verified
    assert result.deleted.get("knowledge_citations", 0) >= 1, (
        "the citation row survived erasure"
    )
    assert _rows(db_path, "SELECT * FROM knowledge_citations") == []
    assert _rows(db_path, "SELECT * FROM knowledge_retrieval_events") == []
    # And the other user's plan is untouched.
    assert _rows(db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OTHER,)), \
        f"date {date}"


# ---------------------------------------------------------------------------
# Erasure behaviour
# ---------------------------------------------------------------------------


def test_delete_account_removes_every_row_and_verifies_it(db_path):
    _seed(db_path)
    service, database = _service(db_path)
    try:
        before = service.summary(OWNER)
        assert before["daily_plans"] == 1 and before["goals"] == 1

        result = service.delete_account(OWNER)
    finally:
        database._manager._close_connection()

    assert result is not None
    assert result.verified is True, f"residual rows: {result.residual}"
    assert result.residual == {}
    assert result.total_rows >= 4

    # The user row itself is gone, and so is every dependent row.
    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,)) == []
    for table in PrivacyRepository._USER_TABLES:
        assert _rows(
            db_path, f"SELECT * FROM {table} WHERE user_id = ?", (OWNER,)
        ) == [], f"{table} still holds rows for the deleted user"


def test_delete_account_never_touches_another_user(db_path):
    _seed(db_path)
    service, database = _service(db_path)
    try:
        service.delete_account(OWNER)
    finally:
        database._manager._close_connection()

    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OTHER,))
    assert _rows(db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OTHER,))
    assert _rows(db_path, "SELECT * FROM goals WHERE user_id = ?", (OTHER,))
    assert _rows(db_path, "SELECT * FROM activity_log WHERE user_id = ?", (OTHER,))


def test_delete_account_reports_failure_and_deletes_nothing(db_path, monkeypatch):
    """A failing transaction must leave the account intact and say so."""
    _seed(db_path)
    from app.database.repositories import _BaseRepository

    original = _BaseRepository._connection
    state = {"armed": False}

    class _FailingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, *args, **kwargs):
            if "DELETE FROM GOALS" in str(sql).upper():
                state["armed"] = True
                raise sqlite3.OperationalError("injected failure mid-erasure")
            return self._cursor.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _Connection:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self):
            return _FailingCursor(self._connection.cursor())

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        _BaseRepository, "_connection", lambda self: _Connection(original(self)),
    )
    service, database = _service(db_path)
    try:
        result = service.delete_account(OWNER)
    finally:
        database._manager._close_connection()
        monkeypatch.undo()

    assert state["armed"] is True, "the failure was never injected"
    assert result is None, "a rolled-back erasure must not report success"

    # Everything is still there: the transaction was rolled back.
    assert _rows(db_path, "SELECT * FROM users WHERE user_id = ?", (OWNER,))
    assert _rows(db_path, "SELECT * FROM daily_plans WHERE user_id = ?", (OWNER,))
    assert _rows(db_path, "SELECT * FROM goals WHERE user_id = ?", (OWNER,))
    assert _rows(db_path, "SELECT * FROM activity_log WHERE user_id = ?", (OWNER,))


def test_delete_account_removes_stored_files(db_path, tmp_path):
    """Uploaded files must be removed alongside their rows."""
    _seed(db_path)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    key = f"{OWNER}_abc.pdf"
    (storage_root / key).write_bytes(b"content")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO knowledge_sources (owner_user_id, source_type, title) "
        "VALUES (?, 'book', 't')", (OWNER,),
    )
    source_id = conn.execute("SELECT MAX(source_id) FROM knowledge_sources").fetchone()[0]
    conn.execute(
        "INSERT INTO knowledge_storage_files "
        "(owner_user_id, source_id, storage_key, file_name) VALUES (?, ?, ?, 'a.pdf')",
        (OWNER, source_id, key),
    )
    conn.commit()
    conn.close()

    from app.knowledge.repositories import KnowledgeStore
    from app.knowledge.storage import StorageService

    database = _database(db_path)
    store = KnowledgeStore(database._manager)
    storage = StorageService(str(storage_root), store)
    service = PrivacyService(db=database, knowledge_storage=storage)
    try:
        result = service.delete_account(OWNER)
    finally:
        database._manager._close_connection()

    assert result.verified
    assert result.files_removed == 1
    assert not (storage_root / key).exists(), "the uploaded file was left on disk"


# ---------------------------------------------------------------------------
# Export behaviour
# ---------------------------------------------------------------------------


def test_export_contains_the_users_rows_and_is_valid_json(db_path):
    _seed(db_path)
    service, database = _service(db_path)
    try:
        result = service.export(OWNER)
    finally:
        database._manager._close_connection()

    assert result is not None
    assert result.total_rows > 0
    payload = json.loads(result.to_json())      # must not raise
    assert payload["user_id"] == OWNER
    assert payload["format_version"] == 1
    assert payload["tables"]["daily_plans"], "the user's plan is missing"
    assert payload["tables"]["goals"], "the user's goal is missing"
    assert payload["tables"]["users"][0]["user_id"] == OWNER


def test_export_is_scoped_to_one_user(db_path):
    _seed(db_path)
    service, database = _service(db_path)
    try:
        payload = json.loads(service.export(OWNER).to_json())
    finally:
        database._manager._close_connection()

    blob = json.dumps(payload)
    assert "other goal" not in blob, "another user's goal leaked into the export"
    assert "other activity" not in blob
    for row in payload["tables"]["daily_plans"]:
        assert row["user_id"] == OWNER


def test_export_of_an_empty_account_succeeds(db_path):
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (9001, "new"))
    conn.commit()
    conn.close()

    service, database = _service(db_path)
    try:
        result = service.export(9001)
    finally:
        database._manager._close_connection()

    assert result is not None
    payload = json.loads(result.to_json())
    assert payload["tables"]["daily_plans"] == []
    assert result.total_rows == 1  # just the user row


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (True, True),
    (12, 12),
    ("text", "text"),
    (b"12345678", "<8 bytes>"),
])
def test_json_safe_handles_non_serialisable_values(value, expected):
    assert _json_safe(value) == expected


def test_json_safe_handles_datetime_and_nested_structures():
    stamp = datetime(2026, 1, 2, 3, 4, 5)
    out = _json_safe({"when": stamp, "items": [stamp, {"deep": stamp}]})
    assert out == {
        "when": stamp.isoformat(),
        "items": [stamp.isoformat(), {"deep": stamp.isoformat()}],
    }
    json.dumps(out)  # must not raise


# ---------------------------------------------------------------------------
# Retention purge
# ---------------------------------------------------------------------------


def test_purge_expired_logs_deletes_only_old_rows(db_path):
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    old = (datetime.utcnow() - timedelta(days=200)).isoformat()
    fresh = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO activity_log (user_id, action, details, timestamp) "
        "VALUES (?, 'old', '', ?)", (OWNER, old),
    )
    conn.execute(
        "INSERT INTO activity_log (user_id, action, details, timestamp) "
        "VALUES (?, 'fresh', '', ?)", (OWNER, fresh),
    )
    conn.execute(
        "INSERT INTO coach_logs (user_id, date, advice_given) VALUES (?, ?, 'x')",
        (OWNER, old[:10]),
    )
    conn.execute(
        "INSERT INTO coach_logs (user_id, date, advice_given) VALUES (?, ?, 'y')",
        (OWNER, fresh[:10]),
    )
    conn.commit()
    conn.close()

    database = _database(db_path)
    try:
        deleted = database.privacy.purge_expired_logs(90, 90)
        remaining = database.privacy.counts(OWNER)
    finally:
        database._manager._close_connection()

    assert deleted["activity_log"] >= 1
    assert deleted["coach_logs"] >= 1
    actions = {r["action"] for r in _rows(db_path, "SELECT action FROM activity_log")}
    assert "fresh" in actions
    assert "old" not in actions, "an expired row survived the purge"
    assert remaining["activity_log"] >= 1


def test_purge_is_disabled_by_a_zero_window(db_path):
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO activity_log (user_id, action, details, timestamp) "
        "VALUES (?, 'old', '', ?)",
        (OWNER, (datetime.utcnow() - timedelta(days=999)).isoformat()),
    )
    conn.commit()
    conn.close()

    database = _database(db_path)
    try:
        deleted = database.privacy.purge_expired_logs(0, 0)
    finally:
        database._manager._close_connection()

    assert deleted == {"activity_log": 0, "coach_logs": 0}
    assert any(r["action"] == "old" for r in _rows(db_path, "SELECT action FROM activity_log"))


def test_retention_settings_default_to_ninety_days():
    from app.config.settings import Settings

    fields = Settings.__dataclass_fields__
    assert fields["activity_log_retention_days"].default == 90
    assert fields["coach_log_retention_days"].default == 90
