"""AUSTRO AI - Dashboard service."""

from __future__ import annotations

from typing import Any, Dict

from app.database.repositories import Database


class DashboardService:
    """Life dashboard statistics."""

    def __init__(self, db: Database):
        self._db = db

    def stats(self, user_id: int) -> Dict[str, Any]:
        return self._db.stats.dashboard(user_id)