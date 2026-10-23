"""AUSTRO AI - Reminder service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.errors import ValidationError
from app.database.repositories import Database

_INVALID_TIME_MESSAGE = "❌ وقت غير صالح. استخدم الصيغة HH:MM مثل 08:30"


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

        An unparseable time is rejected with a validation error. It used to
        fall back to "now", which stored a reminder at an arbitrary time and
        still told the user "your reminder is saved" - a silent wrong result
        for a typo the user could have corrected. The seconds/microseconds of
        "now" are also dropped: a daily job only has hour/minute/second
        resolution and a reminder created at 08:00:37 would fire at :37.
        """
        cleaned = (time_str or "").strip()
        if not cleaned:
            raise ValidationError(_INVALID_TIME_MESSAGE)
        parsed = parse_time_string(cleaned)
        if parsed is None:
            raise ValidationError(_INVALID_TIME_MESSAGE)

        scheduled_time = datetime.now().replace(
            hour=parsed[0], minute=parsed[1], second=0, microsecond=0
        ).isoformat()
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