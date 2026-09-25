"""AUSTRO AI - Reminder service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.errors import ValidationError
from app.database.repositories import Database


def parse_time_string(time_str: str) -> Optional[Tuple[int, int]]:
    """Parse an HH:MM input; returns (hour, minute) or None when invalid."""
    try:
        hour, minute = map(int, time_str.split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    except (TypeError, ValueError):
        return None


class ReminderService:
    """Custom reminder creation + active reminder listing."""

    def __init__(self, db: Database):
        self._db = db

    def active(self, user_id: int) -> List[Dict[str, Any]]:
        return self._db.reminders.list_active(user_id)

    def create_daily_reminder(self, user_id: int, title: Optional[str],
                              message: Optional[str], time_str: str) -> bool:
        """Create a recurring daily custom reminder from an HH:MM string.

        Invalid time strings fall back to "now" (same behaviour as before);
        they never crash the conversation.
        """
        if not time_str or not time_str.strip():
            raise ValidationError("❌ الرجاء إدخال وقت صحيح بصيغة HH:MM")

        now = datetime.now()
        parsed = parse_time_string(time_str)
        scheduled_time = (
            now.replace(hour=parsed[0], minute=parsed[1]).isoformat()
            if parsed
            else now.isoformat()
        )
        reminder_id = self._db.reminders.create(
            user_id=user_id,
            reminder_type="custom",
            title=title,
            message=message,
            scheduled_time=scheduled_time,
            is_recurring=True,
            frequency="daily",
        )
        return reminder_id is not None