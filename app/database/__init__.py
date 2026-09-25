"""Database: SQLite connection lifecycle + repository layer.

Instantiates the single shared instance (`db`) used across the application.
"""

from __future__ import annotations

from app.database.connection import DatabaseManager
from app.database.repositories import (
    ActivityRepository,
    Database,
    GoalRepository,
    HabitRepository,
    PlanRepository,
    ProgressRepository,
    ReminderRepository,
    ReviewRepository,
    StatsRepository,
    UserRepository,
)

_manager = DatabaseManager()
db: Database = Database(_manager)

__all__ = [
    "DatabaseManager",
    "Database",
    "ActivityRepository",
    "GoalRepository",
    "HabitRepository",
    "PlanRepository",
    "ProgressRepository",
    "ReminderRepository",
    "ReviewRepository",
    "StatsRepository",
    "UserRepository",
    "db",
]