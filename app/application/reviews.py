"""AUSTRO AI - Daily review (accountability) service."""

from __future__ import annotations

from typing import Any, Dict, List

from app.database.repositories import Database
from app.domain.entities import DailyReviewData


class ReviewService:
    """Saving and listing daily accountability reviews."""

    def __init__(self, db: Database):
        self._db = db

    def save(self, review: DailyReviewData) -> bool:
        return self._db.reviews.save(
            review.user_id, review.date, review.accomplished, review.learned,
            review.obstacles, review.tomorrow_plan, review.mood,
        )

    def history(self, user_id: int, days: int = 7) -> List[Dict[str, Any]]:
        return self._db.reviews.list(user_id, days)