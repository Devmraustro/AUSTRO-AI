"""AUSTRO AI - Coach engine (Phase E).

One-thing-at-a-time focus: picks the single most useful next action for the
learner today - a due review, the weakest in-progress objective, or the next
objective - plus any blocker standing in the way. Deterministic focus;
optional LLM elaboration is schema-validated and falls back to the template.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.domain.ai import AIRequest
from app.learning.models import CoachFocus
from app.learning.repositories import LearningStore
from app.learning.schemas import (
    COACH_ADVICE_FIELDS,
    extract_json,
    validate_schema,
    valid_coach_advice,
)

logger = logging.getLogger(__name__)


class CoachEngine:
    def __init__(self, store: LearningStore, ai=None,
                 settings: Optional[Dict[str, Any]] = None,
                 today: Optional[Any] = None):
        self._store = store
        self._ai = ai
        self._settings = settings or {}
        self._today = today or (lambda: datetime.utcnow().strftime("%Y-%m-%d"))

    def today(self, owner_user_id: int) -> CoachFocus:
        day = self._today()
        due = self._store.reviews.due(owner_user_id, day)
        focus = self._pick_focus(owner_user_id, due)
        blocker = self._pick_blocker(owner_user_id)
        smallest = self._smallest_action(focus)

        result = CoachFocus(
            focus=focus, blocker=blocker, smallest_action=smallest,
            review_items=[r.get("concept") or (r.get("objective_title") or "")
                          for r in due[:5]],
            reason=self._reason(focus, blocker),
            llm=False,
        )
        if self._use_llm():
            llm_focus = self._llm_focus(owner_user_id, result)
            if llm_focus:
                for key in ("focus", "blocker", "smallest_action", "reason"):
                    if llm_focus.get(key):
                        setattr(result, key, llm_focus[key])
                result.llm = True
        return result

    def _use_llm(self) -> bool:
        if not bool(self._settings.get("learning_use_llm", False)):
            return False
        try:
            return bool(self._ai and self._ai.provider and self._ai.provider.is_available)
        except Exception:
            return False

    def _pick_focus(self, owner_user_id: int, due: List[Dict[str, Any]]) -> str:
        if due:
            return "إكمال المراجعات المتخللة المستحقة"
        active = [m for m in self._store.mastery.list_for_owner(owner_user_id)
                  if m.get("state") not in ("MASTERED", "REGRESSED")]
        if active:
            weakest = sorted(active, key=lambda m: (m.get("score", 0.0),
                                                    m.get("updated_at", "")))[0]
            title = self._objective_title(weakest["objective_id"])
            return f"الاستمرار على هدف «{title}»"
        goals = self._store.goals.list(owner_user_id)
        goal = next((g for g in goals if g.get("kind") == "long_term"), None)
        if goal:
            return f"بدء المسار نحو «{goal.get('title')}»"
        return "تحديد هدف تعليمي أولاً"

    def _pick_blocker(self, owner_user_id: int) -> str:
        misconceptions = self._store.misconceptions.list(owner_user_id)
        severe = [m for m in misconceptions
                  if m.get("severity") == "high" and not m.get("acknowledged")]
        if severe:
            return (severe[0].get("pattern") or "").strip()
        regressed = [m for m in self._store.mastery.list_for_owner(owner_user_id)
                     if m.get("state") == "REGRESSED"]
        if regressed:
            return f"تراجع في «{self._objective_title(regressed[0]['objective_id'])}»"
        return ""

    def _smallest_action(self, focus: str) -> str:
        if "مراجعة" in focus:
            return "أكمل مراجعة واحدة فقط (5 دقائق)."
        return "اقرأ الدرس 10 دقائق ثم أجب عن سؤال سريع."

    def _reason(self, focus: str, blocker: str) -> str:
        if blocker:
            return f"أولوية اليوم: {focus}، والعوائق المعروفة: {blocker}."
        return f"الأفضل اليوم: {focus}."

    def _objective_title(self, objective_id: int) -> str:
        objective = self._store.objectives.get(objective_id)
        return (objective or {}).get("title") or "هدف"

    def _llm_focus(self, owner_user_id: int, result: CoachFocus) -> Optional[Dict[str, Any]]:
        data = {
            "focus": result.focus, "blocker": result.blocker,
            "smallest_action": result.smallest_action,
        }
        request = AIRequest(
            capability="coach_focus",
            prompt=(
                "بواعتماد على وضع المتعلم التالي، حسّن التركيز اليومي: "
                f"{data}\nأعد JSON بالشكل: {{\"focus\": \"\", \"blocker\": \"\", "
                "\"smallest_action\": \"\", \"reason\": \"\"}}"
            ),
            system_prompt="أنت مدرّب تعلم شخصي. أعد JSON فقط.",
            max_tokens=400, temperature=0.3, user_id=owner_user_id,
            data={"focus": result.focus},
        )
        try:
            response = asyncio.new_event_loop().run_until_complete(
                self._ai.generate(request)
            )
            parsed = extract_json(response.text)
            if not isinstance(parsed, dict) or validate_schema(parsed, COACH_ADVICE_FIELDS) or \
                    not valid_coach_advice(parsed):
                logger.warning("LLM coach focus failed validation, using deterministic")
                return None
            return parsed
        except Exception as exc:  # pragma: no cover - fallback safety
            logger.warning(f"LLM coach focus failed: {exc}")
            return None


__all__ = ["CoachEngine"]