"""AUSTRO AI - Diagnostic assessment (Phase E).

Estimates the learner's current level per objective by combining evidence:
measured performance (mastery records), optional self-assessment, and optional
stored learning-state memory. Self-reported skill is never trusted alone; the
result is a compromise weighted toward measured evidence. Deterministic.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from app.learning.models import (
    DiagnosticEntry,
    DiagnosticResult,
)
from app.learning.repositories import LearningStore

_WEIGHT_PERFORMANCE = 0.5
_WEIGHT_SELF = 0.25
_WEIGHT_MEMORY = 0.25


def score_to_status(score: float) -> str:
    if score >= 70:
        return "known"
    if score >= 40:
        return "partially_known"
    if score >= 20:
        return "weak"
    return "unknown"


def overall_level_from_statuses(statuses: List[str]) -> str:
    if not statuses:
        return "beginner"
    points = {"unknown": 0, "weak": 1, "partially_known": 2, "known": 3}
    mean = sum(points.get(s, 0) for s in statuses) / len(statuses)
    if mean >= 2.0:
        return "advanced"
    if mean >= 1.0:
        return "intermediate"
    return "beginner"


class DiagnosticEngine:
    """Combine measured evidence with optional self/memory signals."""

    def __init__(self, store: LearningStore,
                 memory_score: Optional[Callable[[int, str], float]] = None):
        self._store = store
        self._memory_score = memory_score

    def diagnose(self, owner_user_id: int, goal_id: int,
                 self_ratings: Optional[Dict[int, float]] = None) -> DiagnosticResult:
        objectives = self._store.objectives.list_for_goal(goal_id)
        statuses: List[str] = []
        entries: List[DiagnosticEntry] = []
        mastery_records = {
            int(m["objective_id"]): m
            for m in self._store.mastery.list_for_owner(owner_user_id)
        }

        for objective in objectives:
            objective_id = int(objective["objective_id"])
            title = objective.get("title") or ""
            measured = mastery_records.get(objective_id)
            performance = float(measured["score"]) if measured else 0.0
            self_score = float((self_ratings or {}).get(objective_id, -1.0))
            memory_signal = -1.0
            if self._memory_score is not None:
                memory_signal = float(self._memory_score(owner_user_id, title) or 0.0)

            sources = 0
            total_weight = 0.0
            combined = 0.0
            if measured is not None:
                combined += performance * _WEIGHT_PERFORMANCE
                total_weight += _WEIGHT_PERFORMANCE
                sources += 1
            if self_score >= 0:
                combined += self_score * _WEIGHT_SELF
                total_weight += _WEIGHT_SELF
                sources += 1
            if memory_signal >= 0:
                combined += memory_signal * _WEIGHT_MEMORY
                total_weight += _WEIGHT_MEMORY
                sources += 1
            combined = combined / total_weight if total_weight else 0.0

            if measured is not None and measured["evidence_count"] >= 3:
                combined = max(combined, 0.55 * combined + 0.45 * performance)

            status = score_to_status(combined)
            if measured is not None and measured["state"] == "MASTERED":
                status = "known"
            confidence = min(1.0, 0.25 * sources + 0.15 * ((measured or {}).get("evidence_count") or 0))
            statuses.append(status)
            entries.append(DiagnosticEntry(
                objective_id=objective_id, title=title, status=status,
                score=round(combined, 1), confidence=round(confidence, 2),
            ))

        return DiagnosticResult(
            goal_id=goal_id,
            overall_level=overall_level_from_statuses(statuses),
            entries=entries,
        )


__all__ = ["DiagnosticEngine", "score_to_status", "overall_level_from_statuses"]