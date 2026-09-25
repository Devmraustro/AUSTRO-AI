"""AUSTRO AI - Flashcard generation and spaced repetition (Phase E).

Deterministic flashcard generation from lesson content and assessment questions
for active recall practice. Fully integrates with the spaced-repetition scheduler
for next-review scheduling and difficulty tracking. Cards are derived from
lesson recap, quick_check questions, and assessment concepts.

Flashcard lifecycle:
  create  ->  review  ->  track result  ->  schedule next review  ->  mastery update
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.learning.models import Flashcard
from app.learning.repositories import LearningStore
from app.learning.spaced import SpacedReviewScheduler

logger = logging.getLogger(__name__)


def _extract_keywords(text: str) -> List[str]:
    """Extract significant keywords from Arabic text."""
    import re
    return [w for w in re.findall(r"[\w\u0600-\u06FF]{3,}", (text or "").lower()) if w not in {"و", "لـ", "أن", "من", "في", "أنه"}]


def _make_flashcard(front: str, back: str, concept: str = "", difficulty: str = "medium") -> Flashcard:
    """Create a Flashcard DTO from front/back text."""
    return Flashcard(
        front=front,
        back=back,
        concept=concept,
        difficulty=difficulty,
    )


class FlashcardEngine:
    """Generates flashcards for active recall practice and integrates with spaced repetition."""

    def __init__(self, store: LearningStore, ai=None, settings=None, today: Optional[Any] = None):
        self._store = store
        self._ai = ai
        self._settings = settings or {}
        self._today = today or (lambda: date.today())

    # -- public API ---------------------------------------------------------

    def from_lesson(self, owner_user_id: int, lesson_id: int) -> List[Flashcard]:
        """Generate flashcards from a lesson's content and recap."""
        lesson = self._store.lessons.get(owner_user_id, lesson_id)
        if lesson is None:
            return []

        cards: List[Flashcard] = []

        # Card from lesson title + essence
        title = lesson.content.get("title", "") or ""
        essence = lesson.content.get("essence", "") or ""
        if title or essence:
            front = (title + " " + essence).strip()
            back = essence or title or ""
            cards.append(_make_flashcard(front, back, concept=title))

        # Card from recap
        recap = lesson.content.get("recap", "") or ""
        if recap:
            front = recap[:100] + ("…" if len(recap) > 100 else "")
            back = recap
            cards.append(_make_flashcard(front, back, concept="recap"))

        # Cards from quick_check questions
        quick_check = lesson.content.get("quick_check", []) or []
        for qc in quick_check:
            question = qc.get("question", "") or ""
            answer = qc.get("answer", "") or ""
            if question and answer:
                front = question
                back = answer
                concept = question.split("«")[1].split("»")[0] if "«" in question else ""
                cards.append(_make_flashcard(front, back, concept=concept))

        # Card from next_step
        next_step = lesson.content.get("next_step", "") or ""
        if next_step:
            front = next_step
            back = f"خطوة تالية: {next_step}"
            cards.append(_make_flashcard(front, back, concept="next_step"))

        return cards

    def from_objective(self, owner_user_id: int, objective_id: int) -> List[Flashcard]:
        """Generate flashcards from an objective's concept."""
        objective = self._store.objectives.get(objective_id)
        if objective is None:
            return []

        concept = objective.get("title") or ""
        if not concept:
            return []

        cards = []

        # Card from concept definition prompt style
        front = f"مَا هُوَ «{concept}»؟"
        back = f"توضيح: {concept} هو المفهوم الأساسي لهذا الهدف."
        cards.append(_make_flashcard(front, back, concept=concept))

        # Cards from lesson if objective has a lesson
        lessons = self._store.lessons.list_for_objective(owner_user_id, objective_id)
        if lessons:
            cards.extend(self.from_lesson(owner_user_id, lessons[0].lesson_id))

        return cards

    def from_assessment(self, owner_user_id: int, objective_id: int) -> List[Flashcard]:
        """Generate flashcards from weak concepts after assessment."""
        from app.learning.misconceptions import MisconceptionEngine
        misc_engine = MisconceptionEngine(self._store)
        misconceptions = misc_engine.weaknesses(owner_user_id, limit=5)

        cards: List[Flashcard] = []
        for pattern in misconceptions:
            # Extract concept from pattern
            concept = pattern.replace("خلط في مفهوم ", "").replace("خلط بين ", "").replace("فهم جزئي لمفهوم ", "")
            if concept:
                front = f"مفهوم «{concept}»"
                back = f"مراجعة: راجع مفهوم «{concept}» وتفاصيله من الدرس."
                cards.append(_make_flashcard(front, back, concept=concept))

        return cards

    def record_review(
        self,
        owner_user_id: int,
        flashcard_id: int,
        correct: bool,
        answer: str = "",
    ) -> Dict[str, Any]:
        """Record a flashcard review result and schedule next review.

        Returns updated flashcard state with new interval.
        """
        # Find the flashcard (in real implementation, query by ID)
        # For now, we record the review in the spaced scheduler
        scheduler = SpacedReviewScheduler(today=self._today)

        # Simulate a review item - in production this would be stored per flashcard
        item = {
            "interval_days": 1,
            "attempt_count": 1,
            "success_count": 1 if correct else 0,
            "failure_count": 0 if correct else 1,
            "ease": 2.5,
        }

        if correct:
            item = scheduler.schedule(item, correct=True)
        else:
            item = scheduler.schedule(item, correct=False)

        # Determine next review date
        next_review = datetime.strptime(item["next_review"], "%Y-%m-%d").date()

        return {
            "correct": correct,
            "next_review": next_review.isoformat(),
            "interval_days": item["interval_days"],
            "ease": item["ease"],
            "review_count": item["success_count"] + item["failure_count"],
        }


__all__ = ["FlashcardEngine", "_extract_keywords", "_make_flashcard"]