"""AUSTRO AI - Goal service."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.database.repositories import Database
from app.domain.entities import NewGoal


class GoalService:
    """Goal creation, listing and progress updates."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, goal: NewGoal) -> Optional[int]:
        return self._db.goals.create(
            goal.user_id, goal.title, goal.description,
            goal.category, goal.stages, goal.deadline,
        )

    def list(self, user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._db.goals.list(user_id, status)

    def update_progress(self, goal_id: int, progress: int) -> bool:
        return self._db.goals.update_progress(goal_id, progress)