"""AUSTRO AI - Daily plan service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.database.repositories import Database
from app.domain.entities import NewPlan


class PlanService:
    """Daily plan creation and retrieval."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, plan: NewPlan) -> bool:
        return self._db.plans.create(
            plan.user_id, plan.date, plan.tasks, plan.priorities,
            plan.review_time, plan.break_time,
        )

    def for_date(self, user_id: int, date: str) -> Optional[Dict[str, Any]]:
        return self._db.plans.get(user_id, date)