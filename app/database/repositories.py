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


class _BaseRepository:
    """Shared helpers: locked access to the thread-local connection."""

    def __init__(self, manager: DatabaseManager):
        self._manager = manager

    def _connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

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
                cursor.execute(
                    "INSERT OR REPLACE INTO users "
                    "(user_id, username, first_name, last_name, last_active) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, username, first_name, last_name, datetime.now().isoformat()),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in create user: {e}")
            return False

    def update_profile(self, user_id: int, **kwargs: Any) -> bool:
        """Update user profile fields (JSON-encoding list/dict values)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                for key, value in kwargs.items():
                    cursor.execute(
                        f"UPDATE users SET {key} = ? WHERE user_id = ?",
                        (self._serialise(value), user_id),
                    )
                cursor.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?",
                    (datetime.now().isoformat(), user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
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
            logger.error(f"Database error in get_daily_plan: {e}")
            return None


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
            logger.error(f"Database error in get_habits: {e}")
            return []

    def log(self, habit_id: int, user_id: int, date: str,
            completed: bool, note: str = "") -> bool:
        """Log habit completion and update streaks."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
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
            logger.error(f"Database error in log_habit: {e}")
            return False


class ProgressRepository(_BaseRepository):
    def log(self, user_id: int, date: str, **kwargs: Any) -> bool:
        """Log daily progress (upsert by user + date)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT progress_id FROM progress WHERE user_id = ? AND date = ?",
                    (user_id, date),
                )
                existing = cursor.fetchone()
                if existing:
                    fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
                    values = list(kwargs.values()) + [user_id, date]
                    cursor.execute(
                        f"UPDATE progress SET {fields} WHERE user_id = ? AND date = ?",
                        values,
                    )
                else:
                    fields = ", ".join(kwargs.keys())
                    placeholders = ", ".join(["?"] * len(kwargs))
                    values = [user_id, date] + list(kwargs.values())
                    cursor.execute(
                        f"INSERT INTO progress (user_id, date, {fields}) "
                        f"VALUES (?, ?, {placeholders})",
                        values,
                    )
                self._connection().commit()
                return True
        except DB_ERROR as e:
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
            logger.error(f"Database error in get_progress: {e}")
            return []


class ReviewRepository(_BaseRepository):
    def save(self, user_id: int, date: str, accomplished: str, learned: str,
             obstacles: str, tomorrow_plan: str, mood: int) -> bool:
        """Save (upsert) a daily review."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO daily_reviews "
                    "(user_id, date, accomplished, learned, obstacles, tomorrow_plan, mood) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, date, accomplished, learned, obstacles, tomorrow_plan, mood),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
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
            logger.error(f"Database error in get_all_active_reminders: {e}")
            return []


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
            logger.error(f"Database error in log_activity: {e}")
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

    def log_activity(self, user_id: int, action: str, details: str = "") -> bool:
        return self.activity.log(user_id, action, details)

    def get_dashboard_stats(self, user_id: int) -> Dict[str, Any]:
        return self.stats.dashboard(user_id)