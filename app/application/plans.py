"""AUSTRO AI - Daily plan service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.database.repositories import Database
from app.domain.entities import NewPlan


class PlanService:
    """Daily plan creation, retrieval and task completion."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, plan: NewPlan) -> bool:
        return self._db.plans.create(
            plan.user_id, plan.date, plan.tasks, plan.priorities,
            plan.review_time, plan.break_time,
        )

    def for_date(self, user_id: int, date: str) -> Optional[Dict[str, Any]]:
        return self._db.plans.get(user_id, date)

    def today(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Today's plan (UTC date, matching how plans are written)."""
        return self.for_date(user_id, datetime.utcnow().strftime("%Y-%m-%d"))

    def task_progress(self, plan: Dict[str, Any]) -> Dict[str, int]:
        """Return ``{done, total, percent}`` for a plan's tasks."""
        tasks = plan.get("tasks") or []
        done_flags = plan.get("completed_tasks") or []
        done = sum(1 for index, _ in enumerate(tasks) if _is_done(done_flags, index))
        total = len(tasks)
        percent = int(round(done * 100 / total)) if total else 0
        return {"done": done, "total": total, "percent": percent}

    def set_task_done(self, user_id: int, date: str, index: int,
                      done: bool) -> Optional[Dict[str, Any]]:
        """Move one task to an explicit completion state, exactly once.

        Statistics are incremented from the repository's *transition* status
        only ("changed"), never from the request: a duplicate Telegram delivery
        (a retried callback query, or two workers handling the same tap) must
        not count the same completion twice. ``done=False`` reverses a single
        completion, clamped at zero.

        Returns the updated plan with ``just_completed_task`` /
        ``just_reopened_task`` set, or ``None`` when the plan/task is missing.
        """
        outcome = self._db.plans.set_task_state(user_id, date, index, done)
        if outcome is None:
            return None

        if outcome["status"] == "changed":
            self._db.stats.log_completed_task(
                user_id, date, delta=1 if outcome["done"] else -1,
            )

        plan = self.for_date(user_id, date)
        if plan is None:
            return None
        plan["just_completed_task"] = (
            outcome["task"] if outcome["done"] else None)
        plan["just_reopened_task"] = (
            None if outcome["done"] else outcome["task"])
        plan["transition"] = outcome["status"]
        plan.update(self.task_progress(plan))
        return plan

    def all_done(self, plan: Dict[str, Any]) -> bool:
        progress = self.task_progress(plan)
        return progress["total"] > 0 and progress["done"] == progress["total"]


def _normalise_flags(raw: Any, length: int) -> list:
    """Normalise the stored ``completed_tasks`` value to a list of booleans.

    Older rows may hold a JSON object (``{index: true}``), a dict of titles or
    ``null``. Every shape is coerced to ``length`` booleans so a legacy row can
    still be completed from the bot.
    """
    flags = [False] * length
    if isinstance(raw, list):
        for index in range(min(length, len(raw))):
            flags[index] = bool(raw[index])
    elif isinstance(raw, dict):
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= index < length:
                flags[index] = bool(value)
    return flags


def _is_done(flags: Any, index: int) -> bool:
    if isinstance(flags, list):
        return index < len(flags) and bool(flags[index])
    if isinstance(flags, dict):
        return bool(flags.get(index) or flags.get(str(index)))
    return False
