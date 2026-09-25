"""AUSTRO AI - Misconception detection (Phase E).

Deterministic, owner-scoped detection: a wrong answer on an assessed concept
creates (or strengthens) a misconception for that (objective, concept). Built
purely from observed evidence - no LLM - so it is always trustworthy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.learning.models import Misconception
from app.learning.repositories import LearningStore

_PATTERN_POOL = (
    "خلط في مفهوم «{concept}»",
    "خلط بين «{concept}» والمفهوم السابق",
    "فهم جزئي لمفهوم «{concept}»",
)


def pattern_for(concept: str, index: int = 0) -> str:
    if not concept:
        return "فهم جزئي لمفهوم غير محدد"
    return _PATTERN_POOL[index % len(_PATTERN_POOL)].format(concept=concept)


class MisconceptionEngine:
    def __init__(self, store: LearningStore):
        self._store = store

    def detect(self, owner_user_id: int, objective_id: int, concept: str,
               user_answer: str, correct: bool) -> Optional[Misconception]:
        """Record a misconception when an answer is wrong."""
        if correct:
            return None
        pattern = pattern_for(concept)
        self._store.misconceptions.record(
            owner_user_id, objective_id, pattern,
            evidence=_preview(user_answer),
            severity="low",
        )
        row = self._store.misconceptions.get(owner_user_id, objective_id, pattern)
        return _as_misconception(row) if row else None

    def for_owner(self, owner_user_id: int) -> List[Misconception]:
        rows = self._store.misconceptions.list(owner_user_id)
        return _as_misconceptions(rows)

    def weaknesses(self, owner_user_id: int, limit: int = 5) -> List[str]:
        """Short Arabic labels of the highest-severity misconceptions."""
        rows = self._store.misconceptions.list(owner_user_id)
        rows = [r for r in rows if not r.get("acknowledged")]
        rows.sort(key=lambda r: (r.get("count", 1), r.get("severity", "low")),
                  reverse=True)
        out = []
        for row in rows[:limit]:
            label = (row.get("pattern") or "").strip() or \
                (row.get("objective_title") or "مفهوم ما")
            out.append(label)
        return out

    def acknowledge(self, owner_user_id: int, misconception_id: int) -> bool:
        return self._store.misconceptions.acknowledge(owner_user_id, misconception_id)


def _preview(text: str, limit: int = 240) -> str:
    text = (text or "").strip()
    return text[:limit] if len(text) > limit else text


def _as_misconception(row: Dict[str, Any]) -> Misconception:
    known = {f for f in Misconception.__dataclass_fields__}
    item = Misconception(**{k: v for k, v in row.items() if k in known})
    item.evidence = row.get("evidence") or []
    return item


def _as_misconceptions(rows: List[Dict[str, Any]]) -> List[Misconception]:
    return [_as_misconception(r) for r in rows]


__all__ = ["MisconceptionEngine", "pattern_for"]