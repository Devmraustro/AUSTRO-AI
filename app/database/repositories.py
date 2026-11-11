"""
AUSTRO AI - Repository layer.

UI -> Services -> Repositories -> Database connection.

Each repository owns the SQL for one aggregate and returns plain dicts matching
the existing SQLite schema. `Database` is the aggregate facade used by
services, the scheduler and the legacy top-level `db` global.

All data queries live HERE (single implementation); the connection manager in
`connection.py` only manages connections and the schema.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.database.connection import DatabaseManager
from app.database.dialect import DB_ERROR

logger = logging.getLogger(__name__)

_JSON_FIELDS = {
    "users": ["goals", "current_skills", "strengths", "weaknesses"],
    "goals": ["stages"],
    "daily_plans": ["tasks", "priorities", "completed_tasks"],
}

# Identifiers that callers may supply as **kwargs keys. Interpolated into SQL
# only after allowlist validation, so the f-strings below accept schema
# columns and nothing else (prevents SQL injection via key names).
_PROFILE_FIELDS = frozenset({
    "username", "first_name", "last_name", "age", "education_level",
    "goals", "current_skills", "daily_available_time",
    "strengths", "weaknesses", "current_mode",
})

_PROGRESS_FIELDS = frozenset({
    "study_hours", "tasks_completed", "goals_advanced",
    "habits_maintained", "discipline_score", "notes",
})


def _load_json_list(raw: Any) -> List[Any]:
    """Decode a JSON-array column into a list; any bad shape becomes []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _load_flags(raw: Any, length: int) -> List[bool]:
    """Decode ``daily_plans.completed_tasks`` into exactly ``length`` booleans.

    Legacy rows may hold a JSON array, a ``{index: bool}`` object or NULL; every
    shape is coerced so an old plan can still be completed from the bot.
    """
    flags = [False] * length
    if isinstance(raw, list):
        for index in range(min(length, len(raw))):
            flags[index] = bool(raw[index])
        return flags
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= index < length:
                flags[index] = bool(value)
        return flags
    if raw:
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return flags
        return _load_flags(decoded, length)
    return flags


#: Maximum optimistic retries when two workers flip *different* tasks of the
#: same plan at the same moment. Each loser re-reads and re-issues; the loop is
#: bounded so a pathological writer cannot spin a bot worker forever.
_CAS_MAX_ATTEMPTS = 5


def _cas_key(raw: Any) -> Optional[str]:
    """Normalise a raw ``daily_plans.completed_tasks`` value for comparison.

    The column is ``TEXT`` on SQLite *and* PostgreSQL, and legacy rows hold a
    JSON array, a ``{index: bool}`` object, ``''`` or ``NULL``. Instead of
    asking the engine to parse JSON (``json_each`` on SQLite vs ``jsonb`` casts
    on PostgreSQL - not portable), the compare-and-set is done on the *exact
    text that was read*:

    * a non-empty value is compared verbatim, so the UPDATE can only match a row
      that still holds the state this worker saw;
    * ``NULL`` and ``''`` are folded to ``None`` so a brand-new plan (``NULL``)
      and an emptied one (``''``) compare alike.

    Returning ``None`` means "the column is empty", and the statement then uses
    ``IS NULL``/``= ''`` semantics explicitly.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        # A driver may hand back an already-decoded value; re-encode it so the
        # comparison is still against the exact text in the column.
        raw = json.dumps(raw)
    return raw if raw != "" else None


class _BaseRepository:
    """Shared helpers: locked access to the thread-local connection."""

    def __init__(self, manager: DatabaseManager):
        self._manager = manager

    def _connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

    def _rollback(self) -> None:
        """Discard an uncommitted transaction left by a failed statement.

        Every repository method catches ``DB_ERROR`` to degrade gracefully.
        Without an explicit rollback that leaves two problems: on SQLite the
        implicit transaction stays open so a partially-applied write is
        committed later by an unrelated ``commit()``; on PostgreSQL a single
        failed statement poisons the session (``25P02``) and every subsequent
        command fails until a ROLLBACK is issued.

        Must be called while holding ``self._manager._lock`` (every call site is
        inside the same ``with`` block as the statement that failed).
        """
        try:
            self._manager._get_connection().rollback()
        except Exception:  # noqa: BLE001 - rollback must never mask the original error
            pass

    @staticmethod
    def _serialise(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return value

    @staticmethod
    def _deserialise(row: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        for field in fields:
            if row.get(field):
                try:
                    row[field] = json.loads(row[field])
                except (json.JSONDecodeError, TypeError):
                    row[field] = [] if field != "completed_tasks" else {}
        return row


class UserRepository(_BaseRepository):
    def create(self, user_id: int, username: str, first_name: str, last_name: str = "") -> bool:
        """Create or update a user."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                # `INSERT OR REPLACE` on SQLite is DELETE-then-INSERT. Because
                # `users` is the parent of eleven ON DELETE CASCADE foreign keys,
                # re-registering a user wiped every goal, habit, plan, progress
                # row, review, session, reminder and coach log, and reset all
                # profile columns not listed here (including `age`, so the wipe
                # was self-perpetuating on the next /start).
                #
                # `ON CONFLICT ... DO UPDATE` is a true upsert: it updates only
                # the named columns, on both SQLite (>= 3.24) and PostgreSQL.
                cursor.execute(
                    "INSERT INTO users "
                    "(user_id, username, first_name, last_name, last_active) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "username = excluded.username, "
                    "first_name = excluded.first_name, "
                    "last_name = excluded.last_name, "
                    "last_active = excluded.last_active",
                    (user_id, username, first_name, last_name, datetime.now().isoformat()),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in create user: {e}")
            return False

    def update_profile(self, user_id: int, **kwargs: Any) -> bool:
        """Update user profile fields (JSON-encoding list/dict values)."""
        unknown = set(kwargs) - _PROFILE_FIELDS
        if unknown:
            raise ValueError(
                f"update_profile does not allow these columns: {sorted(unknown)}"
            )
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                for key, value in kwargs.items():
                    # key was allowlist-validated against _PROFILE_FIELDS above.
                    cursor.execute(
                        f"UPDATE users SET {key} = ? WHERE user_id = ?",  # nosec B608
                        (self._serialise(value), user_id),
                    )
                cursor.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?",
                    (datetime.now().isoformat(), user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in update_profile: {e}")
            return False

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get a user by ID with JSON fields parsed."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if row:
                    return self._deserialise(dict(row), _JSON_FIELDS["users"])
                return None
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_user: {e}")
            return None

    def all_ids(self) -> List[int]:
        """Get all registered user IDs."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute("SELECT user_id FROM users")
                return [row["user_id"] for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_all_user_ids: {e}")
            return []


class GoalRepository(_BaseRepository):
    def create(self, user_id: int, title: str, description: str,
               category: str, stages: List[str], deadline: Optional[str] = None) -> Optional[int]:
        """Create a new goal and return its ID."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO goals (user_id, title, description, category, stages, deadline) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, title, description, category,
                     json.dumps(stages, ensure_ascii=False), deadline),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in create_goal: {e}")
            return None

    def list(self, user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get user goals, optionally filtered by status."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if status:
                    cursor.execute(
                        "SELECT * FROM goals WHERE user_id = ? AND status = ? "
                        "ORDER BY created_at DESC",
                        (user_id, status),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC",
                        (user_id,),
                    )
                goals = []
                for row in cursor.fetchall():
                    goal = dict(row)
                    if goal.get("stages"):
                        goal["stages"] = self._deserialise(goal, ["stages"]).get("stages", [])
                    goals.append(goal)
                return goals
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_goals: {e}")
            return []

    def update_progress(self, goal_id: int, progress: int) -> bool:
        """Update goal progress percentage."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                status = "completed" if progress >= 100 else "active"
                cursor.execute(
                    "UPDATE goals SET progress = ?, status = ? WHERE goal_id = ?",
                    (progress, status, goal_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in update_goal_progress: {e}")
            return False


class PlanRepository(_BaseRepository):
    def create(self, user_id: int, date: str, tasks: List[Dict[str, Any]],
               priorities: List[str], review_time: str, break_time: str) -> bool:
        """Create a daily plan."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO daily_plans (user_id, date, tasks, priorities, review_time, break_time) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, date,
                     json.dumps(tasks, ensure_ascii=False),
                     json.dumps(priorities, ensure_ascii=False),
                     review_time, break_time),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in create_daily_plan: {e}")
            return False

    def get(self, user_id: int, date: str) -> Optional[Dict[str, Any]]:
        """Get the daily plan for a specific date."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM daily_plans WHERE user_id = ? AND date = ?",
                    (user_id, date),
                )
                row = cursor.fetchone()
                if row:
                    plan = dict(row)
                    fallback = []
                    for field in ["tasks", "priorities", "completed_tasks"]:
                        if plan.get(field):
                            try:
                                plan[field] = json.loads(plan[field])
                            except (json.JSONDecodeError, TypeError):
                                plan[field] = [] if field != "completed_tasks" else {}
                        elif field in ("tasks", "priorities"):
                            plan[field] = fallback
                    return plan
                return None
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_daily_plan: {e}")
            return None

    def set_task_state(self, user_id: int, date: str, index: int,
                       done: bool) -> Optional[Dict[str, Any]]:
        """Move one task to an explicit state, exactly once - across processes.

        WHY THIS EXISTS (delivered-callback idempotency + atomicity)
        -----------------------------------------------------------
        Telegram retries a callback query when the bot does not answer fast
        enough, and several application workers (each with its own Python
        process, its own ``Database`` instance and therefore its own
        ``threading.RLock``) can handle the same tap concurrently. Two
        properties are required and neither can be provided by a Python lock:

        1. **Exactly one successful transition.** Two workers that both read
           "not done" and both write "done" must not both report a transition,
           otherwise ``progress.tasks_completed`` is incremented twice.
        2. **Atomicity of state + accounting.** ``daily_plans.completed_tasks``
           and ``progress.tasks_completed`` must move together - a failure
           after one of them must leave neither applied.

        Implementation
        --------------
        The transition is a single **conditional UPDATE**::

            UPDATE daily_plans
               SET completed_tasks = <json with index flipped>, status = ?
             WHERE user_id = ? AND date = ?
               AND COALESCE(completed_tasks, '') = ?

        The ``WHERE`` clause carries the *exact text* this worker read (see
        ``_cas_key``), so the storage engine - not Python - decides the race:
        ``cursor.rowcount`` is 1 only for the statement that actually replaced
        the old state, and 0 for every worker that arrived after it. This is
        plain SQL-92 (``UPDATE``, ``COALESCE``, ``SET``/``WHERE``) and therefore
        identical on SQLite and PostgreSQL; no ``RETURNING``, no ``json_each``,
        no ``group_concat``/``string_agg`` and no engine-specific branch is
        needed, so there is no dialect that can silently diverge.

        ``rowcount == 0`` is not always a lost *transition*: it can also mean
        two workers flipped two *different* tasks at the same time, in which
        case the losing worker's intended state was never applied. The loop
        therefore re-reads and re-issues a bounded number of times
        (``_CAS_MAX_ATTEMPTS``); if the target state is already in place, the
        answer is ``already`` - a repeated callback never counts twice no matter
        which branch it takes.

        The plan write and the accounting write share ONE transaction that is
        committed here, right after the accounting ``INSERT ... ON CONFLICT``
        (``ON CONFLICT`` on ``(user_id, date)`` is supported by SQLite 3.24+ and
        PostgreSQL 9.5+, and the ``progress`` table already carries the
        ``uq_progress_user_date`` unique index that the clause needs), so on any
        failure both are rolled back.

        Returns ``None`` when the plan or the task index does not exist,
        otherwise:
          * ``{"status": "changed", "done": <bool>, "task": <title>,
               "flags": [...]}``
          * ``{"status": "already", "done": <bool>, "task": <title>,
               "flags": [...]}``      - the task is provably in the requested
               state; this call did not move it
          * ``{"status": "contended", "done": <bool>, "task": <title>,
               "flags": [...]}``      - the state is UNKNOWN/unchanged after
               ``_CAS_MAX_ATTEMPTS`` attempts because the row kept changing;
               nothing was applied and the caller may retry
        Only ``changed`` represents a real transition; statistics are written
        from that status alone. ``already`` is never reported without a read
        that proves the requested state, and ``contended`` never claims one.
        """
        try:
            with self._manager._lock:
                connection = self._connection()
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT tasks, completed_tasks FROM daily_plans "
                    "WHERE user_id = ? AND date = ?",
                    (user_id, date),
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                def _col(name: str, position: int):
                    return row[name] if isinstance(row, dict) else row[position]

                tasks = _load_json_list(_col("tasks", 0))
                if not 0 <= index < len(tasks):
                    return None

                task = tasks[index]
                title = (task.get("task") or task.get("title")
                         if isinstance(task, dict) else str(task))

                # Optimistic compare-and-set. Each attempt re-reads the raw
                # column and issues ONE conditional UPDATE; the engine grants it
                # to exactly one writer, so a repeated or concurrent callback
                # can never produce two transitions.
                for _attempt in range(_CAS_MAX_ATTEMPTS):
                    raw_flags = _col("completed_tasks", 1)
                    stored = _load_flags(raw_flags, len(tasks))
                    if stored[index] == bool(done):
                        return {"status": "already", "done": bool(done),
                                "task": title, "flags": stored}

                    flags = list(stored)
                    flags[index] = bool(done)
                    status = "completed" if flags and all(flags) else "pending"
                    expected = _cas_key(raw_flags)

                    # Two COMPLETE literal statements rather than a built-up
                    # string: no SQL is ever assembled at runtime, so the
                    # injection surface of a string-concatenated query is absent
                    # (and static checkers see plain constants).
                    if expected is None:
                        # Empty/NULL column: match only rows still empty.
                        cursor.execute(
                            "UPDATE daily_plans SET completed_tasks = ?, status = ? "
                            "WHERE user_id = ? AND date = ? "
                            "AND COALESCE(completed_tasks, '') = ''",
                            (json.dumps(flags), status, user_id, date),
                        )
                    else:
                        cursor.execute(
                            "UPDATE daily_plans SET completed_tasks = ?, status = ? "
                            "WHERE user_id = ? AND date = ? "
                            "AND COALESCE(completed_tasks, '') = ?",
                            (json.dumps(flags), status, user_id, date, expected),
                        )
                    if cursor.rowcount == 1:
                        break  # this worker owns the transition

                    # Lost the race. Either the same task was just flipped by
                    # another worker (=> "already" on the next read) or a
                    # different task changed and our state was never applied
                    # (=> retry with fresh text). Re-read and decide.
                    connection.rollback()
                    cursor.execute(
                        "SELECT tasks, completed_tasks FROM daily_plans "
                        "WHERE user_id = ? AND date = ?",
                        (user_id, date),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    tasks = _load_json_list(_col("tasks", 0))
                    if not 0 <= index < len(tasks):
                        return None
                    task = tasks[index]
                    title = (task.get("task") or task.get("title")
                             if isinstance(task, dict) else str(task))
                else:
                    # Attempts exhausted without this worker ever owning the
                    # transition. This does NOT prove the task reached the
                    # requested state: another writer may still be flipping it,
                    # and the last compare-and-set may have been rejected
                    # because the row kept changing under us. Reporting
                    # "already" here would claim a success nobody verified, so
                    # the outcome is decided by a real read (and only a read):
                    #   * target state present  -> "already" (genuinely applied)
                    #   * target state absent   -> "contended" (nothing applied,
                    #                              safe to retry)
                    verified = self._read_flags(user_id, date, len(tasks))
                    if verified is None:
                        return None
                    if verified[index] == bool(done):
                        return {"status": "already", "done": bool(done),
                                "task": title, "flags": verified}
                    return {"status": "contended", "done": bool(done),
                            "task": title, "flags": verified}

                # Accounting in the SAME transaction, then a single commit.
                # The +1 only ever happens for a not-done -> done transition,
                # which this statement has just owned.
                delta = 1 if done else -1
                cursor.execute(
                    "INSERT INTO progress (user_id, date, tasks_completed) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT (user_id, date) DO UPDATE SET "
                    "tasks_completed = MAX(COALESCE(progress.tasks_completed, 0) + ?, 0)",
                    (user_id, date, max(delta, 0), delta),
                )
                connection.commit()
                return {"status": "changed", "done": bool(done),
                        "task": title, "flags": flags}
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in set_task_state: {e}")
            return None

    def _read_flags(self, user_id: int, date: str,
                    length: int) -> Optional[List[bool]]:
        """Read the current completion flags (helper for the lost-race path)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT completed_tasks FROM daily_plans "
                    "WHERE user_id = ? AND date = ?",
                    (user_id, date),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                raw = row["completed_tasks"] if isinstance(row, dict) else row[0]
                return _load_flags(raw, length)
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in _read_flags: {e}")
            return None

    def set_completed(self, user_id: int, date: str,
                      completed: List[bool]) -> bool:
        """Persist per-task completion flags for one user's plan.

        Scoped by ``user_id`` as well as ``date`` so a stale callback from
        another account can never mark someone else's tasks. ``completed`` is
        stored as a JSON array parallel to ``tasks``.
        """
        try:
            with self._manager._lock:
                flags = [bool(value) for value in completed]
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE daily_plans SET completed_tasks = ? "
                    "WHERE user_id = ? AND date = ?",
                    (json.dumps(flags), user_id, date),
                )
                updated = cursor.rowcount
                if updated:
                    status = "completed" if flags and all(flags) else "pending"
                    cursor.execute(
                        "UPDATE daily_plans SET status = ? WHERE user_id = ? AND date = ?",
                        (status, user_id, date),
                    )
                self._connection().commit()
                return updated > 0
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in set_completed_tasks: {e}")
            return False


class HabitRepository(_BaseRepository):
    def create(self, user_id: int, name: str, description: str,
               frequency: str = "daily", reminder_time: str = "08:00") -> Optional[int]:
        """Create a new habit and return its ID."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO habits (user_id, name, description, frequency, reminder_time) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, description, frequency, reminder_time),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in create_habit: {e}")
            return None

    def list(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all user habits."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM habits WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_habits: {e}")
            return []

    def log(self, habit_id: int, user_id: int, date: str,
            completed: bool, note: str = "") -> bool:
        """Log habit completion and update streaks (idempotent per day).

        One row per (habit_id, date) is enforced by uq_habit_logs_habit_date
        (migration core v2). Re-submitting the same day with the same outcome
        is a retry: it succeeds without touching streaks a second time.
        Re-submitting with the opposite outcome corrects that day's row and
        adjusts the streak exactly once for the change.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT log_id, completed FROM habit_logs "
                    "WHERE habit_id = ? AND date = ?",
                    (habit_id, date),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if bool(existing[1]) == bool(completed):
                        self._connection().commit()
                        return True
                    cursor.execute(
                        "UPDATE habit_logs SET completed = ?, note = ? WHERE log_id = ?",
                        (completed, note, existing[0]),
                    )
                    if completed:
                        cursor.execute(
                            "UPDATE habits SET current_streak = current_streak + 1, "
                            "total_completions = total_completions + 1, "
                            "longest_streak = MAX(longest_streak, current_streak + 1) "
                            "WHERE habit_id = ?",
                            (habit_id,),
                        )
                    else:
                        cursor.execute(
                            "UPDATE habits SET current_streak = 0, "
                            "total_completions = MAX(total_completions - 1, 0) "
                            "WHERE habit_id = ?",
                            (habit_id,),
                        )
                    self._connection().commit()
                    return True
                cursor.execute(
                    "INSERT INTO habit_logs (habit_id, user_id, date, completed, note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (habit_id, user_id, date, completed, note),
                )
                if completed:
                    cursor.execute(
                        "UPDATE habits SET current_streak = current_streak + 1, "
                        "total_completions = total_completions + 1, "
                        "longest_streak = MAX(longest_streak, current_streak + 1) "
                        "WHERE habit_id = ?",
                        (habit_id,),
                    )
                else:
                    cursor.execute("UPDATE habits SET current_streak = 0 WHERE habit_id = ?",
                                   (habit_id,))
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in log_habit: {e}")
            return False


class ProgressRepository(_BaseRepository):
    def log(self, user_id: int, date: str, **kwargs: Any) -> bool:
        """Log daily progress (upsert by user + date)."""
        unknown = set(kwargs) - _PROGRESS_FIELDS
        if unknown:
            raise ValueError(
                f"log_progress does not allow these columns: {sorted(unknown)}"
            )
        try:
            with self._manager._lock:
                if not kwargs:
                    return True
                cursor = self._connection().cursor()
                # A single ON CONFLICT upsert instead of SELECT-then-INSERT.
                # The read-modify-write below was only serialised within this
                # process by `_lock`; a second process (or another thread that
                # bypassed the lock) could interleave between the SELECT and
                # the INSERT. With `uq_progress_user_date` (CORE_V1) that race
                # no longer silently duplicates or drops the day's row.
                # keys were allowlist-validated against _PROGRESS_FIELDS above.
                columns = ", ".join(kwargs.keys())
                placeholders = ", ".join(["?"] * len(kwargs))
                assignments = ", ".join([f"{k} = excluded.{k}" for k in kwargs.keys()])
                cursor.execute(
                    f"INSERT INTO progress (user_id, date, {columns}) "  # nosec B608
                    f"VALUES (?, ?, {placeholders}) "
                    f"ON CONFLICT (user_id, date) DO UPDATE SET {assignments}",
                    [user_id, date] + list(kwargs.values()),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in log_progress: {e}")
            return False

    def list(self, user_id: int, days: int = 30) -> List[Dict[str, Any]]:
        """Get progress for the last N days."""
        try:
            with self._manager._lock:
                cutoff = (datetime.utcnow().date() - timedelta(days=days)).strftime("%Y-%m-%d")
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM progress WHERE user_id = ? AND date >= ? "
                    "ORDER BY date DESC",
                    (user_id, cutoff),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_progress: {e}")
            return []


class ReviewRepository(_BaseRepository):
    def save(self, user_id: int, date: str, accomplished: str, learned: str,
             obstacles: str, tomorrow_plan: str, mood: int) -> bool:
        """Save (upsert) a daily review."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                # A daily review is one row per (user_id, date). The table had
                # no unique constraint, so `INSERT OR REPLACE` could never
                # conflict and every save appended another row, which made
                # `get_daily_reviews` and the streak/stat queries wrong.
                # CORE_V1 adds the unique index this upsert depends on.
                cursor.execute(
                    "INSERT INTO daily_reviews "
                    "(user_id, date, accomplished, learned, obstacles, tomorrow_plan, mood) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (user_id, date) DO UPDATE SET "
                    "accomplished = excluded.accomplished, "
                    "learned = excluded.learned, "
                    "obstacles = excluded.obstacles, "
                    "tomorrow_plan = excluded.tomorrow_plan, "
                    "mood = excluded.mood",
                    (user_id, date, accomplished, learned, obstacles, tomorrow_plan, mood),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in save_daily_review: {e}")
            return False

    def list(self, user_id: int, days: int = 7) -> List[Dict[str, Any]]:
        """Get recent daily reviews."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM daily_reviews WHERE user_id = ? ORDER BY date DESC LIMIT ?",
                    (user_id, days),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_daily_reviews: {e}")
            return []


class ReminderRepository(_BaseRepository):
    def create(self, user_id: int, reminder_type: str, title: str, message: str,
               scheduled_time: str, is_recurring: bool = False,
               frequency: Optional[str] = None) -> Optional[int]:
        """Create a reminder."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO reminders (user_id, type, title, message, scheduled_time, "
                    "is_recurring, frequency) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, reminder_type, title, message, scheduled_time,
                     is_recurring, frequency),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in create_reminder: {e}")
            return None

    def list_active(self, user_id: int) -> List[Dict[str, Any]]:
        """Get active reminders for a user."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND is_active = TRUE "
                    "ORDER BY scheduled_time",
                    (user_id,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_active_reminders: {e}")
            return []

    def list_all_active(self) -> List[Dict[str, Any]]:
        """Get all active reminders across all users."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute("SELECT * FROM reminders WHERE is_active = TRUE ORDER BY scheduled_time")
                return [dict(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_all_active_reminders: {e}")
            return []

    def record_failed_delivery(self, reminder_id: int, max_attempts: int) -> Optional[int]:
        """Atomically count a failed one-off delivery and retire at the limit."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE reminders SET "
                    "delivery_attempts = COALESCE(delivery_attempts, 0) + 1, "
                    "is_active = CASE "
                    "WHEN COALESCE(delivery_attempts, 0) + 1 >= ? THEN FALSE "
                    "ELSE is_active END "
                    "WHERE reminder_id = ? AND is_active = TRUE",
                    (max_attempts, reminder_id),
                )
                if cursor.rowcount != 1:
                    self._connection().commit()
                    return None
                cursor.execute(
                    "SELECT delivery_attempts FROM reminders WHERE reminder_id = ?",
                    (reminder_id,),
                )
                row = cursor.fetchone()
                self._connection().commit()
                return int(row[0]) if row is not None else None
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error recording reminder delivery failure: {e}")
            return None

    def deactivate(self, reminder_id: int) -> bool:
        """Retire a reminder so it is never delivered again.

        Nothing in the application used to write ``is_active``. A one-off
        reminder therefore stayed active forever and the hourly resync kept
        re-creating a job for a time that had already passed, so a single
        "remind me at 20:00" was re-sent every hour for the lifetime of the
        deployment. This is the missing lifecycle transition.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE reminders SET is_active = FALSE WHERE reminder_id = ?",
                    (reminder_id,),
                )
                self._connection().commit()
                return cursor.rowcount == 1
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in deactivate_reminder: {e}")
            return False


class ActivityRepository(_BaseRepository):
    def log(self, user_id: int, action: str, details: str = "") -> bool:
        """Log user activity."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO activity_log (user_id, action, details) VALUES (?, ?, ?)",
                    (user_id, action, details),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in log_activity: {e}")
            return False


class CoachLogRepository(_BaseRepository):
    def add(self, user_id: int, date: Optional[str],
            performance_analysis: str = "",
            mistakes_identified: str = "",
            improvements_suggested: str = "",
            advice_given: str = "") -> bool:
        """Persist one coach analysis for ``user_id``.

        ``date`` is a DATE column holding ``YYYY-MM-DD``. ``None`` means today
        in UTC; the value is computed in Python because SQLite's ``date('now')``
        has no PostgreSQL equivalent.
        """
        log_date = date or datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO coach_logs "
                    "(user_id, date, performance_analysis, mistakes_identified, "
                    "improvements_suggested, advice_given) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, log_date, performance_analysis,
                     mistakes_identified, improvements_suggested, advice_given),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in add_coach_log: {e}")
            return False


class StatsRepository(_BaseRepository):
    _DEFAULTS = {
        "total_goals": 0,
        "completed_goals": 0,
        "total_habits": 0,
        "total_streaks": 0,
        "weekly_study_hours": 0,
        "weekly_tasks": 0,
        "total_reviews": 0,
    }

    def log_completed_task(self, user_id: int, date: str, delta: int = 1) -> bool:
        """Adjust the number of completed tasks recorded for one day.

        ``progress.tasks_completed`` is what the dashboard, the weekly summary
        and the coach read, so completing a plan task has to update it - but
        never by double counting: re-tapping a task that is already done sends
        ``delta=0`` from the caller, and un-completing it subtracts, clamped at
        zero so a legacy row cannot go negative.
        """
        if delta == 0:
            return True
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if delta > 0:
                    cursor.execute(
                        "INSERT INTO progress (user_id, date, tasks_completed) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT (user_id, date) DO UPDATE SET "
                        "tasks_completed = COALESCE(progress.tasks_completed, 0) + ?",
                        (user_id, date, delta, delta),
                    )
                else:
                    cursor.execute(
                        "UPDATE progress SET tasks_completed = "
                        "MAX(COALESCE(tasks_completed, 0) + ?, 0) "
                        "WHERE user_id = ? AND date = ?",
                        (delta, user_id, date),
                    )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in log_completed_task: {e}")
            return False

    def dashboard(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive dashboard statistics."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed "
                    "FROM goals WHERE user_id = ?",
                    (user_id,),
                )
                goals_stats = cursor.fetchone()

                cursor.execute("SELECT COUNT(*) as total FROM habits WHERE user_id = ?",
                               (user_id,))
                habits_total = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COALESCE(SUM(current_streak), 0) as total_streak "
                    "FROM habits WHERE user_id = ?",
                    (user_id,),
                )
                total_streak = cursor.fetchone()[0]

                week_cutoff = (datetime.utcnow().date() - timedelta(days=7)).strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT COALESCE(SUM(study_hours), 0) as total_hours, "
                    "COALESCE(SUM(tasks_completed), 0) as total_tasks "
                    "FROM progress WHERE user_id = ? AND date >= ?",
                    (user_id, week_cutoff),
                )
                week_progress = cursor.fetchone()

                cursor.execute(
                    "SELECT COUNT(*) as total FROM daily_reviews WHERE user_id = ?",
                    (user_id,),
                )
                reviews_count = cursor.fetchone()[0]

                return {
                    "total_goals": goals_stats["total"] or 0,
                    "completed_goals": goals_stats["completed"] or 0,
                    "total_habits": habits_total or 0,
                    "total_streaks": total_streak or 0,
                    "weekly_study_hours": week_progress["total_hours"] or 0,
                    "weekly_tasks": week_progress["total_tasks"] or 0,
                    "total_reviews": reviews_count or 0,
                }
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in get_dashboard_stats: {e}")
            return dict(self._DEFAULTS)


class Database:
    """Aggregate facade over the SQLite connection + repositories.

    This is the single object used by services, the scheduler and the legacy
    top-level `db` global. Connection internals (`db_path`, `_local`,
    `init_database`, ...) are delegated to the underlying manager so tests can
    re-point the shared instance at a temp file exactly as before.
    """

    def __init__(self, manager: DatabaseManager):
        self._manager = manager
        self.users = UserRepository(manager)
        self.goals = GoalRepository(manager)
        self.plans = PlanRepository(manager)
        self.habits = HabitRepository(manager)
        self.progress = ProgressRepository(manager)
        self.reviews = ReviewRepository(manager)
        self.reminders = ReminderRepository(manager)
        self.activity = ActivityRepository(manager)
        self.coach_logs = CoachLogRepository(manager)
        self.stats = StatsRepository(manager)

    # ---- connection passthrough (kept for test/tooling compatibility) ----
    @property
    def db_path(self) -> str:
        return self._manager.db_path

    @db_path.setter
    def db_path(self, value: str) -> None:
        self._manager.db_path = value

    @property
    def _local(self) -> Any:
        return self._manager._local

    @property
    def _lock(self) -> Any:
        return self._manager._lock

    def _get_connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

    def _close_connection(self) -> None:
        self._manager._close_connection()

    def init_database(self) -> None:
        self._manager.init_database()

    # ---- legacy data-method surface (delegates to repositories) ----
    def create_user(self, user_id: int, username: str, first_name: str,
                    last_name: str = "") -> bool:
        return self.users.create(user_id, username, first_name, last_name)

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.users.get(user_id)

    def update_profile(self, user_id: int, **kwargs: Any) -> bool:
        return self.users.update_profile(user_id, **kwargs)

    def get_all_user_ids(self) -> List[int]:
        return self.users.all_ids()

    def create_goal(self, user_id: int, title: str, description: str, category: str,
                    stages: List[str], deadline: Optional[str] = None) -> Optional[int]:
        return self.goals.create(user_id, title, description, category, stages, deadline)

    def get_goals(self, user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.goals.list(user_id, status)

    def update_goal_progress(self, goal_id: int, progress: int) -> bool:
        return self.goals.update_progress(goal_id, progress)

    def create_daily_plan(self, user_id: int, date: str, tasks: List[Dict[str, Any]],
                          priorities: List[str], review_time: str, break_time: str) -> bool:
        return self.plans.create(user_id, date, tasks, priorities, review_time, break_time)

    def get_daily_plan(self, user_id: int, date: str) -> Optional[Dict[str, Any]]:
        return self.plans.get(user_id, date)

    def set_daily_plan_completed(self, user_id: int, date: str,
                                 completed: List[bool]) -> bool:
        return self.plans.set_completed(user_id, date, completed)

    def set_daily_plan_task_state(self, user_id: int, date: str, index: int,
                                  done: bool) -> Optional[Dict[str, Any]]:
        return self.plans.set_task_state(user_id, date, index, done)

    def log_completed_task(self, user_id: int, date: str, delta: int = 1) -> bool:
        return self.stats.log_completed_task(user_id, date, delta)

    def create_habit(self, user_id: int, name: str, description: str,
                     frequency: str = "daily", reminder_time: str = "08:00") -> Optional[int]:
        return self.habits.create(user_id, name, description, frequency, reminder_time)

    def get_habits(self, user_id: int) -> List[Dict[str, Any]]:
        return self.habits.list(user_id)

    def log_habit(self, habit_id: int, user_id: int, date: str,
                  completed: bool, note: str = "") -> bool:
        return self.habits.log(habit_id, user_id, date, completed, note)

    def log_progress(self, user_id: int, date: str, **kwargs: Any) -> bool:
        return self.progress.log(user_id, date, **kwargs)

    def get_progress(self, user_id: int, days: int = 30) -> List[Dict[str, Any]]:
        return self.progress.list(user_id, days)

    def save_daily_review(self, user_id: int, date: str, accomplished: str, learned: str,
                          obstacles: str, tomorrow_plan: str, mood: int) -> bool:
        return self.reviews.save(user_id, date, accomplished, learned, obstacles,
                                 tomorrow_plan, mood)

    def get_daily_reviews(self, user_id: int, days: int = 7) -> List[Dict[str, Any]]:
        return self.reviews.list(user_id, days)

    def create_reminder(self, user_id: int, reminder_type: str, title: str, message: str,
                        scheduled_time: str, is_recurring: bool = False,
                        frequency: Optional[str] = None) -> Optional[int]:
        return self.reminders.create(user_id, reminder_type, title, message,
                                     scheduled_time, is_recurring, frequency)

    def get_active_reminders(self, user_id: int) -> List[Dict[str, Any]]:
        return self.reminders.list_active(user_id)

    def get_all_active_reminders(self) -> List[Dict[str, Any]]:
        return self.reminders.list_all_active()

    def deactivate_reminder(self, reminder_id: int) -> bool:
        """Mark a (delivered) one-off reminder inactive."""
        return self.reminders.deactivate(reminder_id)

    def record_reminder_delivery_failure(self, reminder_id: int,
                                        max_attempts: int) -> Optional[int]:
        """Count one failed one-off delivery; retire it at the configured limit."""
        return self.reminders.record_failed_delivery(reminder_id, max_attempts)

    def log_activity(self, user_id: int, action: str, details: str = "") -> bool:
        return self.activity.log(user_id, action, details)

    def get_dashboard_stats(self, user_id: int) -> Dict[str, Any]:
        return self.stats.dashboard(user_id)