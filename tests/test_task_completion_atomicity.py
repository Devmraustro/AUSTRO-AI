"""Atomic task-completion transitions - cross-connection and cross-process.

WHY THIS FILE EXISTS
====================
The first version of the compare-and-set in ``PlanRepository.set_task_state``
looked correct and passed every thread-based test, for two reasons that both
hid real defects:

1. ``threading.RLock`` is per ``DatabaseManager`` instance. Two *processes*
   (the real deployment: several bot workers) never share it, so the lock
   guaranteed nothing across them. Two workers could both read "not done",
   both write "done", both report a transition, and
   ``progress.tasks_completed`` was incremented twice.
2. The state write and the accounting write were two separate transactions
   (``set_task_state`` committed, then ``StatsRepository.log_completed_task``
   committed again). A failure in between left a plan row marked complete with
   an uncounted statistic.

The tests below therefore use **independent connections and real OS processes**,
never a shared lock, and they assert the accounting invariant directly:

    progress.tasks_completed == number of distinct not-done -> done transitions
"""

import multiprocessing as mp
import os
import sqlite3
import threading

import pytest

from datetime import datetime

from app.database.connection import DatabaseManager
from app.database.repositories import Database

OWNER = 7001


# ---------------------------------------------------------------------------
# Fixtures: a real temporary SQLite database with an independent manager
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    """Create a fully migrated database file and return its path."""
    from app.database.migrations import apply_migrations

    path = str(tmp_path / "atomic.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from app.database.connection import _CORE_INDEXES_DDL, _CORE_TABLES_DDL

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
    """A brand-new facade with its OWN lock and its OWN connections."""
    manager = DatabaseManager()
    manager.db_path = path
    return Database(manager)


TODAY = datetime.utcnow().strftime("%Y-%m-%d")


def _seed(path: str, tasks=("A", "B"), owner: int = OWNER, date: str = TODAY):
    database = _database(path)
    database.create_user(owner, "u", "T", "")
    database.plans.create(
        owner, date, [{"task": title} for title in tasks], [], "20:00", "10",
    )
    database._manager._close_connection()
    return date


def _weekly_tasks(path: str, owner: int = OWNER) -> int:
    database = _database(path)
    try:
        return database.stats.dashboard(owner)["weekly_tasks"]
    finally:
        database._manager._close_connection()


def _flags(path: str, date: str, owner: int = OWNER):
    database = _database(path)
    try:
        plan = database.plans.get(owner, date)
        return None if plan is None else plan.get("completed_tasks")
    finally:
        database._manager._close_connection()


def _transition(path: str, index: int, done: bool, date: str, owner: int = OWNER):
    database = _database(path)
    try:
        outcome = database.plans.set_task_state(owner, date, index, done)
        return None if outcome is None else outcome["status"]
    finally:
        database._manager._close_connection()


class _FailingConnection:
    """Connection proxy that fails once, between the two writes of a transition.

    ``sqlite3.Cursor`` is an immutable C type, so its ``execute`` cannot be
    monkeypatched; wrapping the *connection* is the supported way to inject a
    failure at an exact point of the repository's transaction.
    """

    def __init__(self, connection, trigger_on, state):
        self._connection = connection
        self._trigger_on = trigger_on
        self._state = state

    def cursor(self):
        return _FailingCursor(self._connection.cursor(), self._trigger_on, self._state)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _FailingCursor:
    def __init__(self, cursor, trigger_on, state):
        self._cursor = cursor
        self._trigger_on = trigger_on
        self._state = state

    def execute(self, sql, *args, **kwargs):
        if not self._state["armed"] and sql.lstrip().upper().startswith("UPDATE DAILY_PLANS"):
            # Arm only once the compare-and-set ran, so the failure lands
            # exactly between the state write and the accounting write.
            self._state["armed"] = True
        elif self._state["armed"] and self._trigger_on in sql.upper():
            self._state["armed"] = False
            raise sqlite3.OperationalError("injected failure between writes")
        return self._cursor.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def _install_failure(monkeypatch, trigger="INSERT INTO PROGRESS"):
    """Make the next transition's accounting write blow up."""
    from app.database.repositories import _BaseRepository

    state = {"armed": False}
    original = _BaseRepository._connection

    def failing_connection(self):
        return _FailingConnection(original(self), trigger, state)

    monkeypatch.setattr(_BaseRepository, "_connection", failing_connection)
    return state


# ---------------------------------------------------------------------------
# 1. Sequential repetition
# ---------------------------------------------------------------------------


def test_repeated_completion_is_counted_once(db_path):
    date = _seed(db_path)

    statuses = [_transition(db_path, 0, True, date) for _ in range(5)]

    assert statuses[0] == "changed"
    assert statuses[1:] == ["already"] * 4
    assert _weekly_tasks(db_path) == 1
    assert _flags(db_path, date) == [True, False]


def test_reopen_then_complete_again_counts_once_per_transition(db_path):
    date = _seed(db_path)

    assert _transition(db_path, 0, True, date) == "changed"
    assert _weekly_tasks(db_path) == 1

    # Re-opening is itself a transition and reverses the credit exactly once.
    assert _transition(db_path, 0, False, date) == "changed"
    assert _transition(db_path, 0, False, date) == "already"
    assert _weekly_tasks(db_path) == 0

    # Completing again is a NEW transition and is credited again.
    assert _transition(db_path, 0, True, date) == "changed"
    assert _transition(db_path, 0, True, date) == "already"
    assert _weekly_tasks(db_path) == 1
    assert _flags(db_path, date) == [True, False]


def test_two_milestones_credit_one_each(db_path):
    date = _seed(db_path)

    assert _transition(db_path, 1, True, date) == "changed"
    assert _transition(db_path, 0, True, date) == "changed"
    assert _transition(db_path, 1, True, date) == "already"
    assert _transition(db_path, 0, True, date) == "already"
    assert _weekly_tasks(db_path) == 2
    assert _flags(db_path, date) == [True, True]


def test_missing_plan_or_index_is_rejected(db_path):
    date = _seed(db_path)

    assert _transition(db_path, 5, True, date) is None          # index out of range
    assert _transition(db_path, -1, True, date) is None         # negative index
    assert _transition(db_path, 0, True, "1999-01-01") is None  # no such plan
    assert _weekly_tasks(db_path) == 0


def test_transition_never_crosses_users(db_path):
    date = _seed(db_path)
    other = OWNER + 1
    database = _database(db_path)
    database.create_user(other, "o", "O", "")
    database._manager._close_connection()

    assert _transition(db_path, 0, True, date, owner=other) is None
    assert _flags(db_path, date, owner=OWNER) in (None, [False, False])
    assert _weekly_tasks(db_path, owner=other) == 0
    assert _weekly_tasks(db_path, owner=OWNER) == 0


# ---------------------------------------------------------------------------
# 2. Independent connections, threads (each thread gets its OWN manager)
# ---------------------------------------------------------------------------


def test_independent_connections_race_yields_exactly_one_change(db_path):
    """The defect: a per-instance ``RLock`` does not protect across instances."""
    date = _seed(db_path)
    workers = 8
    barrier = threading.Barrier(workers)
    statuses = []
    lock = threading.Lock()

    def worker():
        database = _database(db_path)  # its own manager => its own RLock
        try:
            barrier.wait(timeout=30)
            outcome = database.plans.set_task_state(OWNER, date, 0, True)
            with lock:
                statuses.append(outcome["status"])
        finally:
            database._manager._close_connection()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(statuses) == workers
    assert statuses.count("changed") == 1, f"multiple transitions: {statuses}"
    assert _weekly_tasks(db_path) == 1
    assert _flags(db_path, date) == [True, False]


def test_independent_connections_race_between_complete_and_reopen(db_path):
    """Concurrent complete + reopen must stay consistent, never double-credit."""
    date = _seed(db_path)
    barrier = threading.Barrier(6)
    statuses = []
    lock = threading.Lock()

    def worker(done: bool):
        database = _database(db_path)
        try:
            barrier.wait(timeout=30)
            outcome = database.plans.set_task_state(OWNER, date, 0, done)
            with lock:
                statuses.append((done, outcome["status"]))
        finally:
            database._manager._close_connection()

    threads = [threading.Thread(target=worker, args=(index % 2 == 0,))
               for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    flags = _flags(db_path, date)
    counted = _weekly_tasks(db_path)
    assert flags is not None
    # The counter must equal the number of currently-completed tasks: the two
    # directions are exact inverses, so no interleaving can leave a surplus.
    assert counted == (1 if flags[0] else 0), (
        f"counter {counted} inconsistent with flags {flags} (statuses={statuses})"
    )


def test_independent_connections_flipping_different_tasks_never_lose_a_write(
    db_path,
):
    """The CAS must not turn a different-task race into a lost update.

    ``rowcount == 0`` also happens when another worker flipped a *different*
    task. The bounded retry re-reads and re-issues, so both completions apply
    and both are counted - repeated to catch an unlucky interleaving.
    """
    date = _seed(db_path)

    for trial in range(10):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE daily_plans SET completed_tasks = NULL, status = 'pending' "
            "WHERE user_id = ? AND date = ?", (OWNER, date),
        )
        conn.execute("DELETE FROM progress WHERE user_id = ?", (OWNER,))
        conn.commit()
        conn.close()

        managers = []
        for _ in range(2):
            manager = DatabaseManager()
            manager.db_path = db_path
            managers.append(manager)
        barrier = threading.Barrier(2)
        statuses = {}

        def worker(tag, manager, index):
            database = Database(manager)
            barrier.wait(timeout=30)
            statuses[tag] = database.plans.set_task_state(
                OWNER, date, index, True,
            )["status"]

        threads = [
            threading.Thread(target=worker, args=(index, managers[index], index))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for manager in managers:
            manager._close_connection()

        assert sorted(statuses.values()) == ["changed", "changed"], (
            f"trial {trial}: one worker's completion was lost: {statuses}"
        )
        assert _flags(db_path, date) == [True, True], f"trial {trial}"
        assert _weekly_tasks(db_path) == 2, f"trial {trial}"


# ---------------------------------------------------------------------------
# 3. Real OS processes - separate locks AND separate SQLite connections
# ---------------------------------------------------------------------------


def _process_worker(db_path: str, date: str, index: int, done: bool, queue) -> None:
    """Child process: build an independent Database and run one transition."""
    database = _database(db_path)
    try:
        outcome = database.plans.set_task_state(OWNER, date, index, done)
        queue.put(None if outcome is None else outcome["status"])
    except BaseException as exc:  # noqa: BLE001 - surfaced to the parent as a failure
        import traceback

        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            with open(os.path.join(os.path.dirname(db_path), "child_error.log"), "a") as handle:
                handle.write(detail + "\n")
        except OSError:
            pass
        queue.put(f"error:{type(exc).__name__}:{exc}")
    finally:
        database._manager._close_connection()


def test_separate_processes_race_yields_exactly_one_change(db_path, monkeypatch):
    """Four OS processes, four independent SQLite connections, one transition."""
    date = _seed(db_path)
    # ``spawn`` re-imports this module in every child. Importing the module
    # imports ``app.database``, which builds the shared singleton and runs
    # migrations against ``$DB_PATH``; point it at THIS test's isolated database
    # so a child can never touch (or trip over) the suite-wide file.
    monkeypatch.setenv("DB_PATH", db_path)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_process_worker, args=(db_path, date, 0, True, queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0, f"child failed with {process.exitcode}"

    statuses = [queue.get(timeout=10) for _ in processes]
    assert not any(str(status).startswith("error") for status in statuses), statuses
    assert statuses.count("changed") == 1, f"multiple transitions: {statuses}"
    assert _weekly_tasks(db_path) == 1
    assert _flags(db_path, date) == [True, False]


# ---------------------------------------------------------------------------
# 4. Atomicity: state + accounting commit together or not at all
# ---------------------------------------------------------------------------


def test_failure_after_state_write_rolls_back_the_whole_transition(
    db_path, monkeypatch,
):
    """Inject a failure AFTER the plan UPDATE, before the commit.

    Requirement: "rollback for all changes of the operation". The plan row must
    stay untouched and the statistic must not move.
    """
    date = _seed(db_path)
    state = _install_failure(monkeypatch)

    database = _database(db_path)
    try:
        outcome = database.plans.set_task_state(OWNER, date, 0, True)
    finally:
        database._manager._close_connection()
        monkeypatch.undo()

    assert state["armed"] is False, "the failure was never injected"
    assert outcome is None, "a failed transaction must not report success"
    assert _flags(db_path, date) in (None, [False, False]), (
        "the plan row kept a state write from a failed transaction"
    )
    assert _weekly_tasks(db_path) == 0
    # And the database is still usable: a retry succeeds and counts once.
    assert _transition(db_path, 0, True, date) == "changed"
    assert _weekly_tasks(db_path) == 1


def test_retry_after_a_failure_does_not_duplicate_accounting(db_path, monkeypatch):
    """A retried transition after a rollback must be counted exactly once."""
    date = _seed(db_path)
    _install_failure(monkeypatch)

    database = _database(db_path)
    try:
        assert database.plans.set_task_state(OWNER, date, 0, True) is None
    finally:
        database._manager._close_connection()
        monkeypatch.undo()

    # Retry (twice - Telegram may redeliver) and verify a single credit.
    assert _transition(db_path, 0, True, date) == "changed"
    assert _transition(db_path, 0, True, date) == "already"
    assert _weekly_tasks(db_path) == 1


def test_accounting_row_is_created_when_absent(db_path):
    """The completion must work before any progress row exists for the day."""
    date = _seed(db_path)
    database = _database(db_path)
    try:
        assert database.get_progress(OWNER, days=30) == []
    finally:
        database._manager._close_connection()

    assert _transition(db_path, 0, True, date) == "changed"

    database = _database(db_path)
    try:
        rows = database.get_progress(OWNER, days=3650)
    finally:
        database._manager._close_connection()
    assert [row["date"] for row in rows] == [date]
    assert rows[0]["tasks_completed"] == 1


def test_reopening_an_incomplete_task_is_a_noop(db_path):
    """Re-opening a task that was never completed is not a transition."""
    date = _seed(db_path)

    assert _transition(db_path, 0, False, date) == "already"
    assert _weekly_tasks(db_path) == 0
    # A never-ticked plan keeps completed_tasks NULL, i.e. "no task done".
    assert _flags(db_path, date) in (None, [False, False])


def test_reopen_never_drives_the_counter_negative(db_path):
    """Legacy row: task marked done with the counter still at 0.

    Re-opening it must clamp at zero instead of storing -1 (the accounting
    statement uses ``MAX(..., 0)``, which PostgreSQL spells ``GREATEST``).
    """
    date = _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE daily_plans SET completed_tasks = ? WHERE user_id = ? AND date = ?",
        ("[true, false]", OWNER, date),
    )
    conn.execute(
        "INSERT INTO progress (user_id, date, tasks_completed) VALUES (?, ?, 0)",
        (OWNER, date),
    )
    conn.commit()
    conn.close()

    assert _transition(db_path, 0, False, date) == "changed"
    assert _weekly_tasks(db_path) == 0, "the counter went negative"
    assert _flags(db_path, date) == [False, False]

    # A later real completion still counts once.
    assert _transition(db_path, 0, True, date) == "changed"
    assert _weekly_tasks(db_path) == 1


def test_legacy_flag_shapes_still_transition(db_path):
    """Rows written by older code (NULL / dict) must remain completable."""
    date = _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE daily_plans SET completed_tasks = NULL WHERE user_id = ? AND date = ?",
        (OWNER, date),
    )
    conn.commit()
    conn.close()

    assert _transition(db_path, 0, True, date) == "changed"
    assert _weekly_tasks(db_path) == 1

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE daily_plans SET completed_tasks = ? WHERE user_id = ? AND date = ?",
        ('{"0": true}', OWNER, date),
    )
    conn.commit()
    conn.close()
    # Already complete in the legacy representation: no second credit.
    assert _transition(db_path, 0, True, date) == "already"
    assert _weekly_tasks(db_path) == 1


def test_statement_is_portable_plain_sql(db_path, monkeypatch):
    """The CAS must be plain SQL, valid on both engines.

    A ``RETURNING``/``json_each``/``group_concat`` statement would work on one
    engine only; the whole point of the fix is that the same text is accepted
    by SQLite and PostgreSQL (see ``tests/test_repositories_pg.py`` for the real
    PostgreSQL execution of exactly this path).
    """
    date = _seed(db_path)
    seen = []
    from app.database.repositories import _BaseRepository

    original = _BaseRepository._connection

    class Recorder:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self):
            return _RecordingCursor(self._connection.cursor(), seen)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    class _RecordingCursor:
        def __init__(self, cursor, sink):
            self._cursor = cursor
            self._sink = sink

        def execute(self, sql, *args, **kwargs):
            self._sink.append(sql)
            return self._cursor.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    monkeypatch.setattr(
        _BaseRepository, "_connection", lambda self: Recorder(original(self)),
    )
    database = _database(db_path)
    try:
        database.plans.set_task_state(OWNER, date, 0, True)
    finally:
        database._manager._close_connection()
        monkeypatch.undo()

    cas = [sql for sql in seen if sql.lstrip().upper().startswith("UPDATE DAILY_PLANS")]
    assert cas, "the compare-and-set statement did not run"
    forbidden = ("RETURNING", "JSON_EACH", "GROUP_CONCAT", "STRING_AGG", "::JSON")
    for statement in cas:
        upper = statement.upper()
        for token in forbidden:
            assert token not in upper, f"{token} is not portable: {statement}"
        # The guard that decides the race must be present.
        assert "COALESCE(COMPLETED_TASKS" in upper and "=" in upper, statement
    stats = [sql for sql in seen if "INSERT INTO PROGRESS" in sql.upper()]
    assert stats and "ON CONFLICT" in stats[0].upper(), stats


# ---------------------------------------------------------------------------
# 5. Service level: what the Telegram callback actually receives
# ---------------------------------------------------------------------------


def _service(db_path):
    from app.application.plans import PlanService

    database = _database(db_path)
    return PlanService(database), database


def test_service_announces_a_completion_only_once(db_path):
    """``just_completed_task`` drives the user-facing message.

    A retried callback ("already") must not congratulate the user twice, and a
    reopen must announce the reopen instead of the completion.
    """
    date = _seed(db_path)
    service, database = _service(db_path)
    try:
        first = service.set_task_done(OWNER, date, 0, True)
        assert first["just_completed_task"] == "A"
        assert first["just_reopened_task"] is None
        assert first["transition"] == "changed"

        repeat = service.set_task_done(OWNER, date, 0, True)
        assert repeat["just_completed_task"] is None, "completion announced twice"
        assert repeat["just_reopened_task"] is None
        assert repeat["transition"] == "already"

        reopened = service.set_task_done(OWNER, date, 0, False)
        assert reopened["just_reopened_task"] == "A"
        assert reopened["just_completed_task"] is None

        repeat_reopen = service.set_task_done(OWNER, date, 0, False)
        assert repeat_reopen["just_reopened_task"] is None, "reopen announced twice"
        assert repeat_reopen["just_completed_task"] is None

        again = service.set_task_done(OWNER, date, 0, True)
        assert again["just_completed_task"] == "A"

        assert database.stats.dashboard(OWNER)["weekly_tasks"] == 1
    finally:
        database._manager._close_connection()


def test_service_missing_plan_returns_none(db_path):
    _seed(db_path)
    service, database = _service(db_path)
    try:
        assert service.set_task_done(OWNER, "1999-01-01", 0, True) is None
        assert service.set_task_done(OWNER, "1999-01-01", 0, False) is None
    finally:
        database._manager._close_connection()
