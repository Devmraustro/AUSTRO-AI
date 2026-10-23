"""Regression tests for safe, restartable core schema migrations."""

import sqlite3

import pytest

import app.database.migrations as migrations


def _core_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_reviews (review_id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT)"
    )
    conn.execute(
        "CREATE TABLE progress (progress_id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT)"
    )
    conn.executemany(
        "INSERT INTO daily_reviews(review_id, user_id, date) VALUES (?, ?, ?)",
        [(1, 7, "2026-01-01"), (2, 7, "2026-01-01"), (3, None, "2026-01-01"), (4, None, "2026-01-01")],
    )
    conn.executemany(
        "INSERT INTO progress(progress_id, user_id, date) VALUES (?, ?, ?)",
        [(1, 7, "2026-01-01"), (2, 7, "2026-01-01"), (3, 7, None), (4, 7, None)],
    )
    conn.commit()
    return conn


def test_core_v1_deduplicates_only_complete_keys_and_preserves_null_rows(monkeypatch):
    conn = _core_db()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V1])

    migrations.apply_migrations(conn)

    reviews = conn.execute(
        "SELECT review_id, user_id, date FROM daily_reviews ORDER BY review_id"
    ).fetchall()
    progress = conn.execute(
        "SELECT progress_id, user_id, date FROM progress ORDER BY progress_id"
    ).fetchall()
    assert reviews == [(2, 7, "2026-01-01"), (3, None, "2026-01-01"), (4, None, "2026-01-01")]
    assert progress == [(2, 7, "2026-01-01"), (3, 7, None), (4, 7, None)]
    assert migrations._core_index_unique_state(conn, "uq_daily_reviews_user_date") is True
    assert migrations._core_index_unique_state(conn, "uq_progress_user_date") is True
    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() == (1,)
    conn.close()


def test_core_v1_rejects_same_name_non_unique_index_and_rolls_back(monkeypatch):
    conn = _core_db()
    conn.execute(
        "CREATE INDEX uq_daily_reviews_user_date ON daily_reviews(user_id, date)"
    )
    conn.commit()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V1])

    with pytest.raises(RuntimeError, match="conflicting index definition"):
        migrations.apply_migrations(conn)

    # Data cleanup must not have run before detecting the conflicting index.
    assert conn.execute("SELECT COUNT(*) FROM daily_reviews").fetchone() == (4,)
    assert conn.execute("SELECT COUNT(*) FROM progress").fetchone() == (4,)
    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() is None
    conn.close()



def test_core_v1_rejects_unique_index_with_wrong_table_or_columns(monkeypatch):
    conn = _core_db()
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_daily_reviews_user_date ON unrelated(user_id, date)"
    )
    conn.commit()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V1])

    with pytest.raises(RuntimeError, match="conflicting index definition"):
        migrations.apply_migrations(conn)

    assert conn.execute("SELECT COUNT(*) FROM daily_reviews").fetchone() == (4,)
    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() is None
    conn.close()


def test_core_v1_verification_failure_is_not_recorded(monkeypatch):
    conn = _core_db()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V1])
    original = migrations._verify_core_v1_indexes_unique

    def fail_verification(_conn):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(migrations, "_verify_core_v1_indexes_unique", fail_verification)
    with pytest.raises(RuntimeError, match="injected verification failure"):
        migrations.apply_migrations(conn)

    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() is None
    # Explicit rollback is expected for RuntimeError as well as DB errors.
    assert conn.execute(
        "SELECT COUNT(*) FROM daily_reviews"
    ).fetchone() == (4,)
    monkeypatch.setattr(migrations, "_verify_core_v1_indexes_unique", original)
    conn.close()


def test_core_v3_adds_delivery_attempts_and_preserves_reminders(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "schema_name TEXT PRIMARY KEY, version INTEGER NOT NULL, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE reminders ("
        "reminder_id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, title TEXT, "
        "message TEXT, scheduled_time TIMESTAMP, is_recurring BOOLEAN DEFAULT 0, "
        "frequency TEXT, is_active BOOLEAN DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO reminders "
        "(reminder_id, user_id, type, title, message, scheduled_time, is_active) "
        "VALUES (41, 7, 'custom', 'Keep me', 'existing reminder', "
        "'2026-10-23T08:30:00', 1)"
    )
    conn.execute("INSERT INTO schema_migrations(schema_name, version) VALUES ('core', 2)")
    conn.commit()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V3])

    migrations.apply_migrations(conn)
    assert conn.execute(
        "SELECT reminder_id, message, is_active, delivery_attempts "
        "FROM reminders WHERE reminder_id = 41"
    ).fetchone() == (41, "existing reminder", 1, 0)
    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() == (3,)

    conn.execute("UPDATE reminders SET delivery_attempts = 2 WHERE reminder_id = 41")
    conn.commit()
    migrations.apply_migrations(conn)
    assert conn.execute(
        "SELECT delivery_attempts FROM reminders WHERE reminder_id = 41"
    ).fetchone() == (2,)
    conn.close()


def test_core_v3_recovers_when_column_exists_but_version_is_not_recorded(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "schema_name TEXT PRIMARY KEY, version INTEGER NOT NULL, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE reminders ("
        "reminder_id INTEGER PRIMARY KEY, delivery_attempts INTEGER)"
    )
    conn.execute("INSERT INTO reminders(reminder_id, delivery_attempts) VALUES (1, NULL)")
    conn.execute("INSERT INTO schema_migrations(schema_name, version) VALUES ('core', 2)")
    conn.commit()
    monkeypatch.setattr(migrations, "_DB_ENGINE", "sqlite")
    monkeypatch.setattr(migrations, "MIGRATIONS", [migrations.CORE_V3])

    migrations.apply_migrations(conn)

    assert conn.execute(
        "SELECT delivery_attempts FROM reminders WHERE reminder_id = 1"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'core'"
    ).fetchone() == (3,)
    conn.close()
