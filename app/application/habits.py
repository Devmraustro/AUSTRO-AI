"""AUSTRO AI - Habit service."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.database.repositories import Database
from app.domain.entities import NewHabit


class HabitService:
    """Habit creation and listing."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, habit: NewHabit) -> Optional[int]:
        return self._db.habits.create(
            habit.user_id, habit.name, habit.description,
            habit.frequency, habit.reminder_time,
        )

    def list(self, user_id: int) -> List[Dict[str, Any]]:
        return self._db.habits.list(user_id)