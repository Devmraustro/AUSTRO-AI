"""AUSTRO AI - Adaptive day planner (Phase E).

Builds a realistic daily plan inside a time budget: due spaced reviews first,
then the next active lesson + practice, then optional habit/task items the
caller exposes. Never punishes a user because a previous plan was unrealistic:
when a user has low recent activity the plan automatically adjusts to a
smaller, achievable budget (planned minutes shrink, items drop).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.learning.models import DailyPlan, PlanItem
from app.learning.repositories import LearningStore

_REVIEW_MINUTES_DEFAULT = 5
_SESSION_MINUTES_DEFAULT = 25
_HABIT_MINUTES = 5
_TASK_MINUTES = 10
_MAX_ITEMS_DEFAULT = 8


class AdaptivePlanner:
    def __init__(self, store: LearningStore, settings: Optional[Dict[str, Any]] = None,
                 habits_provider: Optional[Callable[[int, str], List[Dict[str, Any]]]] = None,
                 tasks_provider: Optional[Callable[[int, str], List[Dict[str, Any]]]] = None,
                 today: Optional[Callable[[], str]] = None):
        self._store = store
        self._settings = settings or {}
        self._habits_provider = habits_provider
        self._tasks_provider = tasks_provider
        self._today = today or (lambda: datetime.utcnow().strftime("%Y-%m-%d"))

    # -- configuration ------------------------------------------------------
    def _review_minutes(self) -> int:
        return int(self._settings.get("learning_review_minutes", _REVIEW_MINUTES_DEFAULT))

    def _session_minutes(self) -> int:
        return int(self._settings.get("learning_default_session_minutes",
                                      _SESSION_MINUTES_DEFAULT))

    def _max_items(self) -> int:
        return int(self._settings.get("learning_planner_max_items", _MAX_ITEMS_DEFAULT))

    # -- budget / adaptation ------------------------------------------------
    def _budget(self, owner_user_id: int, time_minutes: Optional[int]) -> int:
        if time_minutes is not None and time_minutes > 0:
            return int(time_minutes)
        # Default budget: a full study block (reviews + lesson) with room.
        return self._review_minutes() + self._session_minutes() + 20

    def _needs_adjustment(self, owner_user_id: int, current_day: str,
                          lookback_days: int = 7) -> bool:
        """Recent days with a plan but zero recorded progress => shrink plan."""
        start = (datetime.strptime(current_day, "%Y-%m-%d") -
                 timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        progress = {p["record_date"] for p in
                    self._store.progress.range(owner_user_id, start, current_day)}
        plans = self._store.plans.recent(owner_user_id, lookback_days)
        if not plans:
            return False
        planned_days = {p["plan_date"] for p in plans}
        active_days = {d for d in planned_days if d in progress}
        planned = len(planned_days)
        if planned == 0:
            return False
        return (len(active_days) / planned) < 0.4

    # -- item providers -----------------------------------------------------
    def _review_items(self, owner_user_id: int, day: str) -> List[PlanItem]:
        due = self._store.reviews.due(owner_user_id, day)
        minutes = max(1, self._review_minutes())
        return [
            PlanItem(kind="review",
                     title=(r.get("objective_title") or r.get("concept") or "مراجعة"),
                     minutes=minutes, target_id=r.get("review_id"),
                     reason="مراجعة متباعدة مستحقة")
            for r in due[:self._max_items()]
        ]

    def _lesson_item(self, owner_user_id: int, day: str) -> Optional[PlanItem]:
        next_obj = self._next_objective(owner_user_id)
        if next_obj is None:
            return None
        return PlanItem(kind="lesson", title=next_obj["title"],
                        minutes=self._session_minutes(),
                        target_id=next_obj["objective_id"],
                        reason="الهدف التعليمي التالي")

    def _next_objective(self, owner_user_id: int) -> Optional[Dict[str, Any]]:
        candidates = [m for m in self._store.mastery.list_for_owner(owner_user_id)
                      if m.get("state") not in ("MASTERED",)]
        if candidates:
            candidates.sort(key=lambda m: (m.get("score", 0.0), m.get("updated_at", "")))
            objective_id = candidates[0]["objective_id"]
        else:
            goals = self._store.goals.list(owner_user_id)
            goal = next((g for g in goals if g.get("kind") == "long_term"), None)
            if goal is None:
                return None
            objectives = self._store.objectives.list_for_goal(goal["goal_id"])
            if not objectives:
                return None
            objective_id = objectives[0]["objective_id"]
        return self._store.objectives.get(objective_id)

    def _habit_items(self, owner_user_id: int, day: str) -> List[PlanItem]:
        if self._habits_provider is None:
            return []
        try:
            habits = self._habits_provider(owner_user_id, day) or []
        except Exception:
            return []
        return [
            PlanItem(kind="habit", title=h.get("title") or "عادة",
                     minutes=int(h.get("minutes", _HABIT_MINUTES)),
                     reason="التزام عادة يومية")
            for h in habits[:2]
        ]

    def _task_items(self, owner_user_id: int, day: str) -> List[PlanItem]:
        if self._tasks_provider is None:
            return []
        try:
            tasks = self._tasks_provider(owner_user_id, day) or []
        except Exception:
            return []
        return [
            PlanItem(kind="task", title=t.get("title") or "مهمة",
                     minutes=_TASK_MINUTES, target_id=t.get("task_id"),
                     reason="مهمة مخطط لها")
            for t in tasks[:2]
        ]

    # -- public API ---------------------------------------------------------
    def plan_day(self, owner_user_id: int, day: Optional[str] = None,
                 time_minutes: Optional[int] = None,
                 force_adjust: bool = False) -> DailyPlan:
        day = day or self._today()
        adjusted = force_adjust or self._needs_adjustment(owner_user_id, day)
        budget = self._budget(owner_user_id, time_minutes)
        if adjusted:
            budget = max(10, int(budget * 0.6))
            self._store.events.log(
                owner_user_id, "plan_adjusted", None, None,
                {"reason": "low recent completion", "original_minutes": budget},
            )

        items: List[PlanItem] = []
        for source_items in (self._review_items(owner_user_id, day),
                             [item for item in [self._lesson_item(owner_user_id, day)] if item],
                             self._habit_items(owner_user_id, day),
                             self._task_items(owner_user_id, day)):
            for item in source_items:
                if len(items) >= self._max_items():
                    break
                items.append(item)

        # If the budget cannot fit the plan, drop from the tail (tasks/habits
        # last) - never punish past failure, just present an achievable plan.
        total = sum(i.minutes for i in items)
        while total > budget and len(items) > 0:
            total -= items.pop().minutes

        plan = DailyPlan(
            owner_user_id=owner_user_id, plan_date=day, items=items,
            source="adaptive_planner",
            total_minutes=total,
            adjusted=adjusted or total > budget,
        )
        self._store.plans.save(
            owner_user_id, day,
            [item.to_dict() for item in items],
            source=plan.source, total_minutes=plan.total_minutes,
            adjusted=plan.adjusted,
        )
        self._store.events.log(
            owner_user_id, "plan_saved", None, None,
            {"date": day, "minutes": plan.total_minutes, "adjusted": plan.adjusted},
        )
        return plan

    def get_plan(self, owner_user_id: int, day: Optional[str] = None) -> Optional[DailyPlan]:
        day = day or self._today()
        row = self._store.plans.get(owner_user_id, day)
        if row is None:
            return None
        return DailyPlan(
            owner_user_id=owner_user_id, plan_date=day,
            items=[PlanItem(**item) for item in row.get("items") or []],
            source=row.get("source") or "adaptive_planner",
            total_minutes=int(row.get("total_minutes") or 0),
            adjusted=bool(row.get("adjusted")),
        )


__all__ = ["AdaptivePlanner"]