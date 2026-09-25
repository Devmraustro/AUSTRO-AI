"""AUSTRO AI - Weekly review (Phase E).

Summarizes the past period (default 7 days): what was planned vs completed,
sessions run, mastery deltas, weak areas, and concrete next-week priorities.
Deterministic by default; when an LLM is enabled the summary/advice can be
generated through a validated schema - invalid output falls back to the
deterministic summary (never fabricates the numbers).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.domain.ai import AIRequest
from app.learning.models import WeeklyReviewResult
from app.learning.repositories import LearningStore
from app.learning.schemas import (
    WEEKLY_REVIEW_FIELDS,
    extract_json,
    validate_schema,
)

logger = logging.getLogger(__name__)

_PLANNED_KINDS = ("review", "lesson")


def _day(offset: int) -> str:
    return (datetime.utcnow() + timedelta(days=offset)).strftime("%Y-%m-%d")


class WeeklyReviewEngine:
    def __init__(self, store: LearningStore, ai=None,
                 settings: Optional[Dict[str, Any]] = None,
                 habit_consistency: Optional[Callable[[int], float]] = None,
                 today: Optional[Callable[[], str]] = None):
        self._store = store
        self._ai = ai
        self._settings = settings or {}
        self._habit_consistency = habit_consistency
        self._today = today or (lambda: datetime.utcnow().strftime("%Y-%m-%d"))

    def weekly(self, owner_user_id: int, days: int = 7) -> WeeklyReviewResult:
        end = self._today()
        start = (datetime.strptime(end, "%Y-%m-%d") -
                 timedelta(days=days)).strftime("%Y-%m-%d")

        plans = self._store.plans.recent(owner_user_id, days)
        planned = sum(1 for p in plans for item in (p.get("items") or [])
                      if item.get("kind") in _PLANNED_KINDS)
        progress = self._store.progress.range(owner_user_id, start, end)
        completed = int(sum(
            (p.get("practice_count") or 0) +
            (p.get("review_count") or 0) +
            (p.get("assessment_count") or 0) for p in progress
        ))

        sessions = [e for e in self._store.events.list(owner_user_id, limit=300)
                    if e.get("action") == "session_completed"
                    and start <= (e.get("created_at") or "")[:10] <= end]

        mastery_deltas: Dict[str, str] = {}
        masteries = self._store.mastery.list_for_owner(owner_user_id)
        for m in masteries:
            if m.get("state") in ("MASTERED", "NEAR_MASTERY", "REGRESSED"):
                title = self._objective_title(m["objective_id"])
                mastery_deltas[title] = m.get("state", "")

        weak = self._weak_areas(owner_user_id)
        improved = [title for title, state in mastery_deltas.items()
                    if state == "MASTERED"]
        failed = [title for title, state in mastery_deltas.items()
                  if state == "REGRESSED"]
        reasons = self._reasons(owner_user_id)
        changes = self._changes(completed, planned, len(improved), failed)
        priorities = self._priorities(owner_user_id, weak)

        result = WeeklyReviewResult(
            period_start=start, period_end=end,
            planned=planned, completed=completed,
            learning_sessions=len(sessions),
            mastery_deltas=mastery_deltas,
            habit_consistency=float(self._habit_consistency(owner_user_id))
            if self._habit_consistency else 0.0,
            weak_areas=weak, improved=improved, failed=failed,
            reasons=reasons, changes=changes, priorities=priorities,
        )

        if self._use_llm():
            advice = self._llm_advice(owner_user_id, result)
            if advice:
                result.reasons = advice.get("reasons") or reasons
                result.changes = advice.get("changes") or changes
                result.priorities = advice.get("priorities") or priorities
        return result

    def _use_llm(self) -> bool:
        if not bool(self._settings.get("learning_use_llm", False)):
            return False
        try:
            return bool(self._ai and self._ai.provider and self._ai.provider.is_available)
        except Exception:
            return False

    def _objective_title(self, objective_id: int) -> str:
        objective = self._store.objectives.get(objective_id)
        return (objective or {}).get("title") or "هدف"

    def _weak_areas(self, owner_user_id: int) -> List[str]:
        misconceptions = self._store.misconceptions.list(owner_user_id)
        unacked = [m for m in misconceptions if not m.get("acknowledged")]
        unacked.sort(key=lambda m: m.get("count", 1), reverse=True)
        labels = [(m.get("pattern") or "").strip() for m in unacked[:3]]
        return [label for label in labels if label]

    def _reasons(self, owner_user_id: int) -> List[str]:
        reasons: List[str] = []
        plans = self._store.plans.recent(owner_user_id, 7)
        if plans and any(p.get("adjusted") for p in plans):
            reasons.append("تم تمديد جدول سابق غير واقعي ليتلاءم مع وقتك المتاح.")
        events = self._store.events.list(owner_user_id, limit=100)
        if any(e.get("action") == "plan_adjusted" for e in events):
            reasons.append("لوحظ تراجع في إتمام المهام فتم تقليص الخطة.")
        if not reasons:
            reasons.append("أداء منتظم؛ استمر بالوتيرة الحالية.")
        return reasons

    def _changes(self, completed: int, planned: int, improved: int,
                 failed: List[str]) -> List[str]:
        changes: List[str] = []
        if completed > 0 and completed >= planned and planned > 0:
            changes.append("زد الجلسة الأولى 10 دقائق هذا الأسبوع.")
        if improved >= 2:
            changes.append("انتقل إلى أهداف أكثر تقدمًا في المواضيع المتقنة.")
        if failed:
            changes.append("ابدأ الأهداف المتراجعة بدرس قصير بدلاً من حذفها.")
        if not changes:
            changes.append("حافظ على الجدولة المتخللة الحالية.")
        return changes

    def _priorities(self, owner_user_id: int, weak: List[str]) -> List[str]:
        priorities: List[str] = []
        due = self._store.reviews.due(owner_user_id, self._today())
        if due:
            priorities.append(f"أكمل {len(due)} مراجعة مستحقة أولاً.")
        if weak:
            priorities.append(f"عالج: {weak[0]}.")
        masteries = sorted(self._store.mastery.list_for_owner(owner_user_id),
                           key=lambda m: m.get("score", 0.0))
        active = [m for m in masteries if m.get("state") not in ("MASTERED",)]
        if active:
            priorities.append(f"واصل: {self._objective_title(active[0]['objective_id'])}.")
        if not priorities:
            priorities.append("حدد هدفًا جديدًا أو راجع أهدافك القديمة.")
        return priorities

    def _llm_advice(self, owner_user_id: int,
                    result: WeeklyReviewResult) -> Optional[Dict[str, Any]]:
        summary = result.__dict__
        request = AIRequest(
            capability="weekly_review",
            prompt=(
                "بناءً على بيانات التعلم التالية أنتج مراجعة أسبوعية: "
                f"{summary}\nأعد JSON بالشكل: {{\"reasons\": [], "
                "\"changes\": [], \"priorities\": []}}"
            ),
            system_prompt="أنت مدرّب تعلم. أعد JSON فقط.",
            max_tokens=600, temperature=0.3, user_id=owner_user_id,
            data={"weak_areas": result.weak_areas},
        )
        try:
            response = asyncio.new_event_loop().run_until_complete(
                self._ai.generate(request)
            )
            data = extract_json(response.text)
            if not isinstance(data, dict) or validate_schema(data, WEEKLY_REVIEW_FIELDS):
                logger.warning("LLM weekly review failed validation, using deterministic")
                return None
            return data
        except Exception as exc:  # pragma: no cover - fallback safety
            logger.warning(f"LLM weekly review failed: {exc}")
            return None


__all__ = ["WeeklyReviewEngine"]