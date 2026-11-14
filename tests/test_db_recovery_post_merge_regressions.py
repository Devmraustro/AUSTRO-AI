"""Post-merge database connection-lifecycle regressions (PR #16 audit).

A PostgreSQL connection that closes (server restart, network blip, idle
timeout) used to stay in the thread-local slot forever. Every later statement
failed and the only recovery was a process restart. These tests pin the two
recovery paths:

* a connection already marked closed is replaced before use;
* a connection whose ROLLBACK itself fails is dropped so the next call
  reconnects.
"""

from __future__ import annotations

from app.database.connection import DatabaseManager
from app.database.repositories import Database


class _FakeConnection:
    def __init__(self, closed: int = 0, rollback_error: bool = False):
        self.closed = closed
        self.rollback_error = rollback_error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = 1

    def rollback(self):
        if self.rollback_error:
            raise RuntimeError("connection already closed")
        return None


def _manager(tmp_path) -> DatabaseManager:
    manager = DatabaseManager(str(tmp_path / "lifecycle.db"))
    manager._close_connection()
    return manager


def test_closed_postgres_connection_is_replaced(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.engine = "postgresql"
    dead = _FakeConnection(closed=2)
    live = _FakeConnection()
    manager._local.connection = dead

    def fake_connect(_db_path=None):
        return live

    import app.database.connection as connection_module

    monkeypatch.setattr(connection_module, "_postgres_connect", fake_connect)

    assert manager._get_connection() is live
    assert dead.close_calls == 1, "the dead connection must be closed"
    assert manager._local.connection is live


def test_live_postgres_connection_is_reused(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.engine = "postgresql"
    live = _FakeConnection()
    manager._local.connection = live
    calls = []

    import app.database.connection as connection_module

    monkeypatch.setattr(
        connection_module, "_postgres_connect", lambda path=None: calls.append(path)
    )

    assert manager._get_connection() is live
    assert calls == []


def test_failed_rollback_discards_the_connection(tmp_path):
    manager = _manager(tmp_path)
    manager.engine = "sqlite"
    broken = _FakeConnection(rollback_error=True)
    manager._local.connection = broken
    database = Database(manager)

    database.privacy._rollback()  # any repository shares the same helper

    assert manager._local.connection is None, (
        "a connection whose rollback failed must not be reused"
    )
    assert broken.close_calls == 1


def test_successful_rollback_keeps_the_connection(tmp_path):
    manager = _manager(tmp_path)
    manager.engine = "sqlite"
    healthy = _FakeConnection()
    manager._local.connection = healthy
    database = Database(manager)

    database.privacy._rollback()

    assert manager._local.connection is healthy
    assert healthy.close_calls == 0


def test_rollback_on_closed_postgres_connection_discards_without_reconnecting(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.engine = "postgresql"
    dead = _FakeConnection(closed=2)
    manager._local.connection = dead
    connect_calls = []

    import app.database.connection as connection_module

    monkeypatch.setattr(
        connection_module,
        "_postgres_connect",
        lambda path=None: connect_calls.append(path),
    )

    database = Database(manager)
    database.privacy._rollback()

    assert connect_calls == [], (
        "_rollback() must not open a new PostgreSQL connection just to roll back"
    )
    assert manager._local.connection is None
    assert dead.close_calls == 1
