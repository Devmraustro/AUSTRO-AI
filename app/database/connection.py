"""
AUSTRO AI - Database connection manager (SQLite + PostgreSQL backends).

Owns the database path, thread-local connections and the schema (DDL).
Contains NO data queries: reading/writing is the job of the repository layer
(app.database.repositories) which talks to a manager instance.

Backends
--------
- SQLite (default): thread-local `sqlite3.Connection` connections, WAL mode,
  `sqlite3.Row` row factory. This is the operational data-access engine for
  local development and the engine exercised by the full test suite.
- PostgreSQL (production): `psycopg2` connections using the PolyglotCursor
  factory (app.database.dialect), which transparently translates the
  SQLite-native repository SQL (? placeholders, INSERT OR REPLACE/IGNORE,
  scalar MAX(...), lastrowid) to PostgreSQL and normalizes result values to the
  same shapes SQLite returns. The repository/handler layer is therefore
  engine-agnostic; PostgreSQL is the production data-access engine.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Optional

from app.config.settings import settings
from app.database.dialect import get_postgresql_cursor_factory, to_postgresql
from app.database.migrations import apply_migrations

logger = logging.getLogger(__name__)

_DB_ENGINE = settings.db_engine

# Core (legacy) schema authored once, translated to PostgreSQL at apply time.
_CORE_TABLES_DDL: list = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        age INTEGER,
        education_level TEXT,
        goals TEXT,
        current_skills TEXT,
        daily_available_time TEXT,
        strengths TEXT,
        weaknesses TEXT,
        current_mode TEXT DEFAULT 'general',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS goals (
        goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT NOT NULL,
        description TEXT,
        category TEXT,
        status TEXT DEFAULT 'active',
        progress INTEGER DEFAULT 0,
        stages TEXT,
        deadline DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date DATE NOT NULL,
        tasks TEXT,
        priorities TEXT,
        review_time TEXT,
        break_time TEXT,
        completed_tasks TEXT,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habits (
        habit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        description TEXT,
        frequency TEXT DEFAULT 'daily',
        current_streak INTEGER DEFAULT 0,
        longest_streak INTEGER DEFAULT 0,
        total_completions INTEGER DEFAULT 0,
        reminder_time TEXT DEFAULT '08:00',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        habit_id INTEGER,
        user_id INTEGER,
        date DATE,
        completed BOOLEAN DEFAULT 0,
        note TEXT,
        FOREIGN KEY (habit_id) REFERENCES habits (habit_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS progress (
        progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date DATE,
        study_hours REAL DEFAULT 0,
        tasks_completed INTEGER DEFAULT 0,
        goals_advanced INTEGER DEFAULT 0,
        habits_maintained INTEGER DEFAULT 0,
        discipline_score INTEGER DEFAULT 0,
        notes TEXT,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_reviews (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date DATE,
        accomplished TEXT,
        learned TEXT,
        obstacles TEXT,
        tomorrow_plan TEXT,
        mood INTEGER,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        subject TEXT,
        topic TEXT,
        duration INTEGER,
        notes TEXT,
        files_summary TEXT,
        test_results TEXT,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS english_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        vocabulary_learned TEXT,
        conversations_practiced INTEGER DEFAULT 0,
        tests_taken INTEGER DEFAULT 0,
        mistakes_corrected TEXT,
        level TEXT DEFAULT 'beginner',
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS programming_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        language TEXT,
        concepts_learned TEXT,
        projects_completed TEXT,
        code_reviews INTEGER DEFAULT 0,
        errors_solved INTEGER DEFAULT 0,
        learning_path TEXT,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cybersecurity_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        topics_learned TEXT,
        labs_completed INTEGER DEFAULT 0,
        certifications_target TEXT,
        tests_passed INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminders (
        reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        title TEXT,
        message TEXT,
        scheduled_time TIMESTAMP,
        is_recurring BOOLEAN DEFAULT 0,
        frequency TEXT,
        is_active BOOLEAN DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coach_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date DATE,
        performance_analysis TEXT,
        mistakes_identified TEXT,
        improvements_suggested TEXT,
        advice_given TEXT,
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

_CORE_INDEXES_DDL: list = [
    "CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_plans_user_date ON daily_plans(user_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_habits_user ON habits(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_habit_logs_habit ON habit_logs(habit_id)",
    "CREATE INDEX IF NOT EXISTS idx_progress_user_date ON progress(user_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_user ON daily_reviews(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id)",
]


def _postgres_connect(db_path: Optional[str] = None):
    """Create a new psycopg2 connection configured from settings."""
    import psycopg2

    cursor_factory = get_postgresql_cursor_factory()
    if cursor_factory is None:
        from psycopg2.extras import DictCursor

        cursor_factory = DictCursor

    conn = psycopg2.connect(
        host=settings.db_host or "localhost",
        port=settings.db_port or 5432,
        dbname=settings.db_name or "austro_ai",
        user=settings.db_user or "austro",
        password=settings.db_password or "",
        connect_timeout=10,
        cursor_factory=cursor_factory,
    )
    conn.set_client_encoding("UTF8")
    return conn


class DatabaseManager:
    """Thread-safe database connection manager (SQLite or PostgreSQL)."""

    def __init__(self, db_path: Optional[str] = None):
        manager_db_path = db_path or settings.db_path
        self.db_path: str = manager_db_path
        self.engine: str = _DB_ENGINE
        self._local = threading.local()
        # RLock: repo methods may nest (e.g. set_consent -> ensure -> get)
        # while already holding the manager lock.
        self._lock = threading.RLock()
        self.init_database()

    def _get_connection(self):
        """Get thread-local database connection for the configured engine."""
        if self.engine == "postgresql":
            if not hasattr(self._local, "connection") or self._local.connection is None:
                self._local.connection = _postgres_connect(self.db_path)
            return self._local.connection

        if not hasattr(self._local, "connection") or self._local.connection is None:
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                logger.info(f"Created database directory: {db_dir}")

            self._local.connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA foreign_keys = ON")
            self._local.connection.execute("PRAGMA journal_mode = WAL")
        return self._local.connection

    def _close_connection(self) -> None:
        """Close thread-local connection."""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None

    def init_database(self) -> None:
        """Initialize all tables and indexes for the configured engine."""
        if self.engine == "postgresql":
            self._init_postgresql()
            return

        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")

        logger.info(f"Database path: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        for statement in _CORE_TABLES_DDL:
            cursor.execute(statement)

        for statement in _CORE_INDEXES_DDL:
            cursor.execute(statement)

        # Versioned schema migrations (knowledge engine, future modules, ...).
        apply_migrations(conn)

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully!")

    def _init_postgresql(self) -> None:
        """Initialize the PostgreSQL schema from the same DDL translated."""
        logger.info(
            f"PostgreSQL: {settings.db_host}:{settings.db_port}/{settings.db_name} "
            f"user={settings.db_user}"
        )
        conn = _postgres_connect(self.db_path)
        try:
            cursor = conn.cursor()
            for statement in _CORE_TABLES_DDL:
                cursor.execute(to_postgresql(statement))
            for statement in _CORE_INDEXES_DDL:
                cursor.execute(to_postgresql(statement))
            conn.commit()
            apply_migrations(conn)
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            table_count = cursor.fetchone()[0]
            conn.commit()
            logger.info(f"PostgreSQL initialized successfully ({table_count} tables)")
        finally:
            conn.close()