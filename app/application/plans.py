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

    def toggle_task(self, user_id: int, date: str, index: int) -> Optional[Dict[str, Any]]:
        """Flip the completion flag of task ``index`` and persist it.

        Returns the updated plan, or ``None`` when the plan/task does not
        exist. Completion is stored in ``daily_plans.completed_tasks`` and the
        number of *newly* completed tasks is added to today's progress row, so
        the dashboard, the weekly summary and the coach all see the same work.
        Re-toggling a task back to "not done" subtracts it again and never
        drives the counter below zero.
        """
        plan = self.for_date(user_id, date)
        if plan is None:
            return None
        tasks = plan.get("tasks") or []
        if not 0 <= index < len(tasks):
            return None

        done_flags = _normalise_flags(plan.get("completed_tasks"), len(tasks))
        was_done = done_flags[index]
        done_flags[index] = not was_done

        updated = self._db.plans.set_completed(user_id, date, done_flags)
        if not updated:
            return None

        task = tasks[index] if isinstance(tasks[index], dict) else {"task": str(tasks[index])}
        self._db.stats.log_completed_task(user_id, date, delta=1 if done_flags[index] else -1)

        plan = self.for_date(user_id, date) or plan
        plan["completed_tasks"] = done_flags
        plan["just_completed_task"] = task.get("task") if done_flags[index] else None
        plan["just_reopened_task"] = None if done_flags[index] else task.get("task")
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
