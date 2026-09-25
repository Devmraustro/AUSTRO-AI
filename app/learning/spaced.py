"""AUSTRO AI - Deterministic spaced-repetition scheduler (Phase E).

A deliberately simple, testable algorithm (SM-0 style interval ladder with a
small ease factor). Every review item tracks concept, difficulty, counts,
interval and next_review date. `today()` is injectable so the schedule is
purely deterministic in tests.

The model is intentionally explicit so it can later evolve (e.g. to a
stability-based model) without changing the callers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

from app.learning.models import REVIEW_INTERVALS

_MAX_INTERVAL_DAYS = 90
_MIN_EASE = 1.3
_MAX_EASE = 3.0
_EASE_STEP = 0.15


def _parse_date(value: Optional[str], fallback: date):
    if not value:
        return fallback
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return fallback


def next_interval(attempt_count: int, previous: int, correct: bool) -> int:
    """Deterministic interval rule (SM-0 ladder, capped)."""
    if not correct:
        return 1
    ladder_index = min(max(attempt_count - 1, 0), len(REVIEW_INTERVALS) - 1)
    ladder = REVIEW_INTERVALS[ladder_index]
    if ladder is not None and ladder > previous:
        return min(ladder, _MAX_INTERVAL_DAYS)
    return min(max(previous * 2, ladder), _MAX_INTERVAL_DAYS)


def next_ease(current: float, correct: bool) -> float:
    if not correct:
        return max(current - _EASE_STEP, _MIN_EASE)
    return min(current + _EASE_STEP, _MAX_EASE)


class SpacedReviewScheduler:
    """Pure scheduling logic over a review item."""

    def __init__(self, today: Optional[Callable[[], date]] = None):
        self._today = today or date.today

    def is_due(self, item: dict, on: Optional[date] = None) -> bool:
        today = on or self._today()
        next_review = _parse_date(item.get("next_review"), today)
        return next_review <= today

    def schedule(self, item: dict, correct: bool,
                 on: Optional[date] = None) -> dict:
        """Return an updated review item row applying one recall result."""
        today = on or self._today()
        attempt_count = int(item.get("attempt_count") or 0) + 1
        success_count = int(item.get("success_count") or 0) + (1 if correct else 0)
        failure_count = int(item.get("failure_count") or 0) + (0 if correct else 1)
        previous = int(item.get("interval_days") or 1)
        interval = next_interval(attempt_count, previous, correct)
        ease = next_ease(float(item.get("ease") or 2.5), correct)
        next_review = today + timedelta(days=interval)
        updated = dict(item)
        updated.update({
            "attempt_count": attempt_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "interval_days": interval,
            "ease": round(ease, 2),
            "last_reviewed": today.isoformat(),
            "next_review": next_review.isoformat(),
        })
        return updated

    def review_weight(self, item: dict, on: Optional[date] = None) -> float:
        """Stable priority for ordering due reviews (higher = earlier)."""
        today = on or self._today()
        next_review = _parse_date(item.get("next_review"), today)
        days_overdue = (today - next_review).days
        failure_count = int(item.get("failure_count") or 0)
        return days_overdue + 1.0 + min(failure_count, 10) * 0.5


__all__ = ["SpacedReviewScheduler", "next_interval", "next_ease"]