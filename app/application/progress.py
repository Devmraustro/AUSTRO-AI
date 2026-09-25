"""AUSTRO AI - Progress service."""

from __future__ import annotations

from typing import Any, Dict

from app.database.repositories import Database


class ProgressService:
    """Weekly progress summaries for the progress menu."""

    def __init__(self, db: Database):
        self._db = db

    def weekly_summary(self, user_id: int) -> Dict[str, Any]:
        rows = self._db.progress.list(user_id, days=7)
        return {
            "total_hours": sum(r.get("study_hours", 0) for r in rows),
            "total_tasks": sum(r.get("tasks_completed", 0) for r in rows),
            "days": len(rows),
        }