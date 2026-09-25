"""AUSTRO AI - Deterministic mastery model (Phase E).

Mastery is evidence-based, never a single correct answer. Each piece of
evidence (assessment/review/practice/quiz result) updates a rolling score and
a recent-consistency window. The next state is a pure function of
(score, evidence_count, consistency_window, distinct kinds, latest result).

States: NEW -> LEARNING -> PRACTICING -> NEAR_MASTERY -> MASTERED,
with REGRESSED when a previously stored skill degrades.
"""

from __future__ import annotations

from typing import Dict, List

from app.learning.models import EVIDENCE_KINDS, MASTERY_STATES

_SCORE_ALPHA = 0.3
_HISTORY_LEN = 10
_WINDOW = 5


def normalize_kind(kind: str) -> str:
    return kind if kind in EVIDENCE_KINDS else "practice"


class MasterySkill:
    """A user's mastery of a single learning objective."""

    def __init__(self, state: str = "NEW", score: float = 0.0,
                 evidence_count: int = 0, recent: List[int] = None,
                 kinds: List[str] = None):
        if state not in MASTERY_STATES:
            state = "NEW"  # pragma: no cover - defensive
        self.state = state
        self.score = float(score)
        self.evidence_count = int(evidence_count)
        self.recent = list(recent) if recent else []
        self.kinds = list(kinds) if kinds else []

    # -- evidence -----------------------------------------------------------
    def record(self, correct: bool, kind: str) -> str:
        """Apply one graded result and return the next state."""
        self.evidence_count += 1
        kind = normalize_kind(kind)
        if kind not in self.kinds:
            self.kinds.append(kind)

        fallback = 0.0 if self.evidence_count <= 1 else self.score
        self.score = (1.0 - _SCORE_ALPHA) * fallback + \
                     _SCORE_ALPHA * (100.0 if correct else 0.0)

        self.recent.append(1 if correct else 0)
        if len(self.recent) > _HISTORY_LEN:
            del self.recent[:-_HISTORY_LEN]

        self.state = transition(
            current=self.state,
            score=self.score,
            evidence_count=self.evidence_count,
            recent=self.recent,
            kinds=self.kinds,
            latest=correct,
        )
        return self.state

    # -- read helpers -------------------------------------------------------
    @property
    def consistency(self) -> float:
        return consistency_of(self.recent, _WINDOW)

    @property
    def distinct_kinds(self) -> int:
        return len(self.kinds)

    def series(self) -> Dict[str, object]:
        return {
            "state": self.state,
            "score": round(self.score, 1),
            "evidence_count": self.evidence_count,
            "recent": list(self.recent),
            "kinds": list(self.kinds),
            "consistency": round(self.consistency, 2),
            "distinct_kinds": self.distinct_kinds,
        }


def consistency_of(recent: List[int], window: int) -> float:
    recent = [r for r in recent if r is not None]
    if not recent:
        return 0.0
    tail = recent[-window:]
    return sum(tail) / len(tail)


def transition(current: str, score: float, evidence_count: int,
               recent: List[int], kinds: List[str], latest: bool) -> str:
    """Pure state-transition rule (fully testable)."""
    consistency = consistency_of(recent, _WINDOW)
    distinct = len(set(kinds))
    mastered_criteria = (
        evidence_count >= 5
        and score >= 85.0
        and consistency >= 0.8
        and distinct >= 2
    )

    if mastered_criteria and current not in ("NEW", "REGRESSED"):
        return "MASTERED"

    if evidence_count >= 5 and score >= 85.0 and distinct >= 2:
        # Close to mastered but consistency wobbled - keep near.
        return max_rank("NEAR_MASTERY", current if current != "REGRESSED" else "LEARNING")

    if evidence_count >= 3 and score >= 70.0 and consistency >= 0.6:
        return max_rank("NEAR_MASTERY", current if current != "REGRESSED" else "LEARNING")

    if evidence_count >= 2 and consistency >= 0.5:
        return max_rank("PRACTICING", current if current != "REGRESSED" else "LEARNING")

    failure_pressure = not latest and consistency < 0.5
    if current in ("NEAR_MASTERY", "MASTERED") and failure_pressure:
        return "REGRESSED"

    if evidence_count >= 1:
        return max_rank("LEARNING", current if current != "REGRESSED" else "LEARNING")

    return "NEW"


def max_rank(*states: str) -> str:
    # REGRESSED sits below LEARNING in progression ranking (it must recover).
    _rank = {"NEW": 0, "REGRESSED": 1, "LEARNING": 2, "PRACTICING": 3,
             "NEAR_MASTERY": 4, "MASTERED": 5}
    return max(states, key=lambda s: _rank.get(s, 0))


__all__ = ["MasterySkill", "consistency_of", "transition", "normalize_kind"]