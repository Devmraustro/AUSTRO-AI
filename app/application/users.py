"""AUSTRO AI - User service (registration + profile)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.errors import ValidationError
from app.database.repositories import Database
from app.domain.context import UserContext

_AGE_MESSAGE = "❌ العمر يجب أن يكون بين 10 و 100. حاول مرة أخرى:"
_NOT_A_NUMBER_MESSAGE = "❌ الرجاء إدخال رقم صحيح:"


class UserService:
    """User registration and profile updates (owns the business rules)."""

    def __init__(self, db: Database):
        self._db = db

    def ensure_user(self, ctx: UserContext) -> bool:
        """Create the user row if this is their first interaction."""
        return self._db.users.create(
            ctx.user_id, ctx.username or "", ctx.first_name, ctx.last_name or ""
        )

    def profile(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Full profile dict (JSON fields parsed) or None."""
        return self._db.users.get(user_id)

    def parse_age(self, raw: str) -> int:
        """Parse and validate an age sent as free text."""
        try:
            age = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_NOT_A_NUMBER_MESSAGE) from exc
        if age < 10 or age > 100:
            raise ValidationError(_AGE_MESSAGE)
        return age

    def update_profile(
        self,
        user_id: int,
        age: Optional[int] = None,
        education_level: Optional[str] = None,
        goals: Optional[list] = None,
        daily_available_time: Optional[str] = None,
    ) -> bool:
        """Apply optional profile fields after validation."""
        fields: Dict[str, Any] = {}
        if age is not None:
            if age < 10 or age > 100:
                raise ValidationError(_AGE_MESSAGE)
            fields["age"] = age
        if education_level is not None:
            fields["education_level"] = education_level
        if goals is not None:
            fields["goals"] = goals
        if daily_available_time is not None:
            fields["daily_available_time"] = daily_available_time
        if not fields:
            return True
        return self._db.users.update_profile(user_id, **fields)