"""AUSTRO AI - AdaptiveLearningEngine facade (Phase E).

The single entry point the presentation layer talks to for adaptive coaching
and the learning engine. Deterministic and owner-scoped; every AI path is
schema-validated and falls back to a deterministic generator when the LLM is
disabled, unavailable or invalid. Never lets the LLM mutate state directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.ai.gateway import AIGateway
from app.learning.assessment import AssessmentEngine
from app.learning.book import BookToCourse
from app.learning.coach import CoachEngine
from app.learning.curriculum import CurriculumEngine
from app.learning.diagnostic import DiagnosticEngine
from app.learning.flashcards import FlashcardEngine, Flashcard
from app.learning.lesson import LessonGenerator
from app.learning.mastery import MasterySkill
from app.learning.misconceptions import MisconceptionEngine
from app.learning.models import (
    Lesson,
    ProgressOverview,
)
from app.learning.planner import AdaptivePlanner
from app.learning.repositories import LearningStore
from app.learning.review import WeeklyReviewEngine
from app.learning.session import SessionManager
from app.learning.spaced import SpacedReviewScheduler

logger = logging.getLogger(__name__)

_DATE_FMT = "%Y-%m-%d"


def _today() -> str:
    return datetime.utcnow().strftime(_DATE_FMT)


class AdaptiveLearningEngine:
    """Composition root for all Phase E learning use cases."""

    def __init__(self, *, store: LearningStore, ai: Optional[AIGateway] = None,
                 settings: Optional[Dict[str, Any]] = None,
                 knowledge: Optional[Any] = None,
                 memory_service: Optional[Any] = None,
                 today: Optional[Any] = None):
        self._store = store
        self._ai = ai
        self._settings = settings or {}
        self._knowledge = knowledge
        self._memory = memory_service
        self._today = today or (lambda: _today())
        self._date = lambda: datetime.strptime(self._today(), _DATE_FMT).date()

        self.curricula = CurriculumEngine(store)
        self.scheduler = SpacedReviewScheduler(today=self._date)
        self.sessions = SessionManager(store)
        self.lessons = LessonGenerator(store, ai, self._settings)
        self.assessments = AssessmentEngine(store, ai)
        self.misconceptions = MisconceptionEngine(store)
        self.diagnostic = DiagnosticEngine(
            store, memory_score=self._memory_score if memory_service else None,
        )
        self.planner = AdaptivePlanner(store, self._settings, today=self._today)
        self.review = WeeklyReviewEngine(store, ai, self._settings,
                                         today=self._today)
        self.coach = CoachEngine(store, ai, self._settings, today=self._today)
        self.books = BookToCourse(store, knowledge, ai, self._settings)
        self.flashcards = FlashcardEngine(store)

    # ------------------------------------------------------------------
    # Memory integration (learning state is only ever recorded when the
    # memory system is enabled for the user).
    # ------------------------------------------------------------------
    def _memory_score(self, owner_user_id: int, subject: str) -> float:
        if self._memory is None:
            return -1.0
        try:
            enabled = self._memory.enabled(owner_user_id)
            if not enabled:
                return -1.0
            notes = self._memory.list(owner_user_id, memory_type="learning_state",
                                      limit=50)
            if not notes:
                return 0.0
            text = " ".join(str(n.get("content") or "") for n in notes)
            tokens = _base_tokens(text)
            subject_tokens = _base_tokens(subject)
            if not subject_tokens:
                return 0.0
            overlap = len(tokens & subject_tokens)
            return min(1.0, 0.5 * overlap / len(subject_tokens))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"memory score failed: {exc}")
            return -1.0

    def _remember_state(self, owner_user_id: int, objective: Dict[str, Any],
                        state: str) -> None:
        if self._memory is None or state not in ("MASTERED", "NEAR_MASTERY"):
            return
        try:
            if not self._memory.enabled(owner_user_id):
                return
            content = f"المستخدم تعلّم «{objective.get('title')}» (حالة: {state})"
            self._memory.record_confirmed(
                owner_user_id, "learning_state", content,
                detail={"objective_id": objective.get("objective_id")},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"memory record failed: {exc}")

    # ------------------------------------------------------------------
    # Flashcards (delegated to FlashcardEngine)
    # ------------------------------------------------------------------
    def from_lesson(self, owner_user_id: int, lesson_id: int) -> List[Flashcard]:
        """Generate flashcards from a lesson's content and recap."""
        return self.flashcards.from_lesson(owner_user_id, lesson_id)

    def from_objective(self, owner_user_id: int, objective_id: int) -> List[Flashcard]:
        """Generate flashcards from an objective's concept."""
        return self.flashcards.from_objective(owner_user_id, objective_id)

    def from_assessment(self, owner_user_id: int, objective_id: int) -> List[Flashcard]:
        """Generate flashcards from weak concepts after assessment."""
        return self.flashcards.from_assessment(owner_user_id, objective_id)

    def record_flashcard_review(
        self,
        owner_user_id: int,
        flashcard_id: int,
        correct: bool,
        answer: str = "",
    ) -> Dict[str, Any]:
        """Record a flashcard review result and schedule next review."""
        return self.flashcards.record_review(owner_user_id, flashcard_id, correct, answer)


# ------------------------------------------------------------------
# Goals & objectives
# ------------------------------------------------------------------
    def create_goal(self, owner_user_id: int, *, kind: str = "long_term",
                    title: str, description: str = "",
                    parent_goal_id: Optional[int] = None,
                    deadline: Optional[str] = None) -> Optional[int]:
        goal_id = self._store.goals.create(
            owner_user_id=owner_user_id, kind=kind, title=title,
            description=description, parent_goal_id=parent_goal_id,
            deadline=deadline,
        )
        if goal_id is not None:
            self._store.events.log(owner_user_id, "goal_created", None, None,
                                   {"kind": kind})
        return goal_id

    def create_goal_chain(self, owner_user_id: int, *, vision: str,
                          long_term: str) -> Dict[str, Any]:
        """Create the vision -> long-term goal hierarchy in one call."""
        vision_id = self.create_goal(owner_user_id, kind="vision", title=vision)
        long_id = self.create_goal(owner_user_id, kind="long_term",
                                   title=long_term, parent_goal_id=vision_id)
        return {"vision_goal_id": vision_id, "long_term_goal_id": long_id}

    def add_objective(self, owner_user_id: int, goal_id: int, *,
                      title: str, description: str = "",
                      difficulty: str = "medium",
                      prerequisites: Optional[List[int]] = None) -> Optional[int]:
        return self._store.objectives.create(
            owner_user_id=owner_user_id, goal_id=goal_id, title=title,
            description=description, difficulty=difficulty,
            prerequisites=prerequisites,
        )

    def goals(self, owner_user_id: int) -> List[Dict[str, Any]]:
        return self._store.goals.list(owner_user_id)

    def objectives(self, owner_user_id: int,
                   goal_id: int) -> List[Dict[str, Any]]:
        return self._store.objectives.list_for_goal(goal_id)

    # ------------------------------------------------------------------
    # Curriculum
    # ------------------------------------------------------------------
    def build_curriculum(self, owner_user_id: int, goal_id: int, *,
                         title: str, mode: str = "READ",
                         objective_ids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
        return self.curricula.build(
            owner_user_id=owner_user_id, goal_id=goal_id, title=title,
            mode=mode, objective_ids=objective_ids,
        )

    def curricula(self, owner_user_id: int, goal_id: int) -> List[Dict[str, Any]]:
        return self.curricula.for_goal(owner_user_id, goal_id)

    def curriculum(self, owner_user_id: int, curriculum_id: int) -> Optional[Dict[str, Any]]:
        return self.curricula.get(owner_user_id, curriculum_id)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnose(self, owner_user_id: int, goal_id: int,
                 self_ratings: Optional[Dict[int, float]] = None) -> Dict[str, Any]:
        result = self.diagnostic.diagnose(owner_user_id, goal_id, self_ratings)
        self._store.events.log(owner_user_id, "diagnostic_ran", None, None,
                               {"goal_id": goal_id,
                                "level": result.overall_level})
        return _as_dict(result)

    # ------------------------------------------------------------------
    # Lessons & sessions
    # ------------------------------------------------------------------
    def next_lesson(self, owner_user_id: int, curriculum_id: int, *,
                    objective_id: Optional[int] = None, mode: Optional[str] = None,
                    grounded: bool = False, force: bool = False) -> Optional[Dict[str, Any]]:
        curriculum = self.curricula.get(owner_user_id, curriculum_id)
        if curriculum is None:
            return None
        mode = mode or curriculum.get("mode") or "READ"
        ordered = self.curricula.ordered_objective_ids(curriculum)

        chosen_id = objective_id
        if chosen_id is None:
            chosen_id = self._next_active_objective(
                owner_user_id, ordered, curriculum.get("goal_id"),
            )
        if chosen_id is None:
            return None
        objective = self._store.objectives.get(chosen_id)
        if objective is None:
            return None

        evidence = []
        if grounded and self._knowledge is not None:
            source_id = curriculum.get("source_id")
            evidence = self._grounded_evidence(owner_user_id, objective, source_id)

        lesson = self.lessons.generate(
            owner_user_id=owner_user_id, curriculum_id=curriculum_id,
            objective_id=chosen_id, mode=mode, evidence=evidence, force=force,
        )
        if lesson is None:
            return None

        resume = self._store.sessions.resume(owner_user_id)
        session = None
        if resume is not None and resume.get("objective_id") == chosen_id:
            self.sessions.attach_lesson(_session_row(resume), lesson.lesson_id)
            session = _session_row(resume)
        else:
            session = self.sessions.start(
                owner_user_id, chosen_id, curriculum.get("goal_id"),
                curriculum_id, mode,
            )
            if session is not None:
                self.sessions.attach_lesson(session, lesson.lesson_id)
        self._store.events.log(
            owner_user_id, "lesson_generated", chosen_id,
            session.session_id if session else None,
            {"grounded": grounded, "origin": _origin_of(lesson)},
        )
        return {
            "lesson": _lesson_dict(lesson),
            "session": _session_dict(session) if session else None,
            "objective": objective,
            "evidence": evidence,
        }

    def continue_session(self, owner_user_id: int) -> Optional[Dict[str, Any]]:
        session = self.sessions.resume(owner_user_id)
        if session is None:
            return None
        result = _session_dict(session)
        if session.lesson_id:
            lesson = self.lessons.get(owner_user_id, session.lesson_id)
            result["lesson"] = _lesson_dict(lesson) if lesson else None
        if session.objective_id:
            result["objective"] = self._store.objectives.get(session.objective_id)
        return result

    def _grounded_evidence(self, owner_user_id: int,
                           objective: Dict[str, Any],
                           source_id: Optional[int]) -> List[Dict[str, Any]]:
        if source_id is None:
            return []
        try:
            return self.books.evidence(owner_user_id, source_id,
                                       objective.get("title") or "", top_k=3)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"grounded evidence failed: {exc}")
            return []

    def _next_active_objective(self, owner_user_id: int, ordered: List[int],
                               goal_id: Optional[int]) -> Optional[int]:
        masteries = {
            int(m["objective_id"]): m
            for m in self._store.mastery.list_for_owner(owner_user_id)
        }
        active = [oid for oid in ordered
                  if (masteries.get(oid) or {}).get("state") not in ("MASTERED",)]
        if active:
            return active[0]
        goals = self._store.goals.list(owner_user_id)
        status = next((g for g in goals if g.get("goal_id") == goal_id), None)
        if status is None:
            return ordered[0] if ordered else None
        return ordered[0] if ordered else None

    # ------------------------------------------------------------------
    # Quick check + assessment
    # ------------------------------------------------------------------
    def quick_check(self, owner_user_id: int, session_id: int,
                    objective_id: int) -> List[Dict[str, Any]]:
        questions = self.assessments.generate_questions(
            owner_user_id=owner_user_id, objective_id=objective_id, count=2,
            kinds=["multiple_choice", "true_false"],
        )
        self._store.events.log(owner_user_id, "quick_check", objective_id,
                               session_id, {"questions": len(questions)})
        return [q.to_dict() for q in questions]

    def submit_quick_check(self, owner_user_id: int, session_id: int,
                           objective_id: int, answers: Dict[int, str],
                           *,
                           questions: Optional[List[Dict[str, Any]]] = None
                           ) -> Dict[str, Any]:
        if not questions:
            questions = self.assessments.generate_questions(
                owner_user_id=owner_user_id, objective_id=objective_id, count=2,
                kinds=["multiple_choice", "true_false"],
            )
        parsed = [_question_from_dict(q) for q in questions]
        result = self.assessments.submit(
            owner_user_id=owner_user_id, objective_id=objective_id,
            questions=parsed, answers=answers, session_id=session_id,
        )
        self._store.progress.record(owner_user_id, self._today(),
                                    practice_count=len(questions))
        if session_id:
            self.sessions.advance(_session_row({"session_id": session_id,
                                                "owner_user_id": owner_user_id}),
                                  "PRACTICE")
        self._remember_state_from(owner_user_id, objective_id)
        return {"result": _as_dict(result),
                "questions": [a.to_dict() for a in parsed]}

    def assessment(self, owner_user_id: int, objective_id: int, *,
                   count: int = 3,
                   kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        objective = self._store.objectives.get(objective_id)
        lesson = None
        if objective is not None:
            goal_id = objective.get("goal_id")
            curricula = self.curricula.for_goal(owner_user_id, goal_id)
            for curriculum in curricula:
                found = self._store.lessons.get_in_curriculum(
                    owner_user_id, curriculum["curriculum_id"], objective_id)
                if found:
                    lesson = found
                    break
        questions = self.assessments.generate_questions(
            owner_user_id=owner_user_id, objective_id=objective_id,
            count=count, kinds=kinds, lesson=lesson,
        )
        return [q.to_dict() for q in questions]

    def submit_assessment(self, owner_user_id: int, objective_id: int, *,
                          questions: List[Dict[str, Any]],
                          answers: Dict[int, str],
                          session_id: Optional[int] = None) -> Dict[str, Any]:
        parsed = [_question_from_dict(q) for q in questions]
        result = self.assessments.submit(
            owner_user_id=owner_user_id, objective_id=objective_id,
            questions=parsed, answers=answers, session_id=session_id,
        )
        if result.weak_concepts and result.score < 90:
            for concept in result.weak_concepts[:3]:
                self._create_review(owner_user_id, objective_id, concept)
        for concept in result.weak_concepts:
            self._store.events.log(owner_user_id, "weak_concept", objective_id,
                                   session_id, {"concept": concept})
        if session_id:
            self.sessions.advance(_session_row({"session_id": session_id,
                                                "owner_user_id": owner_user_id}),
                                  "ASSESS")
        self._remember_state_from(owner_user_id, objective_id)
        return _as_dict(result)

    def _remember_state_from(self, owner_user_id: int,
                             objective_id: int) -> None:
        record = self._store.mastery.get(owner_user_id, objective_id)
        if record is None:
            return
        objective = self._store.objectives.get(objective_id)
        if objective:
            self._remember_state(owner_user_id, objective,
                                 record.get("state", ""))

    # ------------------------------------------------------------------
    # Reviews
    # ------------------------------------------------------------------
    def _create_review(self, owner_user_id: int, objective_id: int,
                       concept: str) -> Optional[int]:
        today = self._today()
        next_review = (datetime.strptime(today, _DATE_FMT) +
                       timedelta(days=1)).strftime(_DATE_FMT)
        return self._store.reviews.create(
            owner_user_id=owner_user_id, objective_id=objective_id,
            concept=concept, next_review=next_review,
        )

    def due_reviews(self, owner_user_id: int) -> List[Dict[str, Any]]:
        return self._store.reviews.due(owner_user_id, self._today())

    def review_prompt(self, owner_user_id: int,
                      review_id: int) -> Optional[str]:
        for item in self._store.reviews.list(owner_user_id):
            if item.get("review_id") == review_id:
                concept = item.get("concept") or \
                    (item.get("objective_title") or "") or "المفهوم"
                return f"استرجاع: راجع «{concept}» بإجابتك بجملة أو أكثر."
        return None

    def record_review(self, owner_user_id: int, review_id: int,
                      correct: bool, answer: str = "") -> Optional[Dict[str, Any]]:
        item = next(
            (r for r in self._store.reviews.list(owner_user_id)
             if r.get("review_id") == review_id), None)
        if item is None:
            return None
        objective_id = int(item["objective_id"])
        updated = self.scheduler.schedule(
            item, correct, on=self._date(),
        )
        self._store.reviews.update(owner_user_id, objective_id,
                                   item.get("concept") or "", updated)

        skill = self._skill(owner_user_id, objective_id)
        skill.record(correct, "review")
        self._persist_skill(owner_user_id, objective_id, skill)

        self._store.progress.record(owner_user_id, self._today(),
                                    review_count=1)
        if not correct:
            self.misconceptions.detect(
                owner_user_id, objective_id,
                item.get("concept") or "عام", answer, correct=False,
            )
        self._store.events.log(owner_user_id, "review_done", objective_id,
                               review_id, {"correct": correct})
        if correct:
            self._remember_state_from(owner_user_id, objective_id)
        return updated

    # ------------------------------------------------------------------
    # Mastery helpers
    # ------------------------------------------------------------------
    def _skill(self, owner_user_id: int, objective_id: int) -> MasterySkill:
        record = self._store.mastery.get(owner_user_id, objective_id)
        if record is None:
            return MasterySkill()
        return MasterySkill(state=record.get("state", "NEW"),
                            score=record.get("score", 0.0),
                            evidence_count=record.get("evidence_count", 0),
                            recent=record.get("recent") or [],
                            kinds=record.get("kinds") or [])

    def _persist_skill(self, owner_user_id: int, objective_id: int,
                       skill: MasterySkill) -> None:
        self._store.mastery.save(
            owner_user_id, objective_id, state=skill.state,
            score=skill.score, evidence_count=skill.evidence_count,
            recent=skill.recent, kinds=skill.kinds,
        )
        self._store.objectives.update_mastery(objective_id, skill.state,
                                              skill.score)

    # ------------------------------------------------------------------
    # Progress & weekly review
    # ------------------------------------------------------------------
    def progress_overview(self, owner_user_id: int) -> ProgressOverview:
        masteries = self._store.mastery.list_for_owner(owner_user_id)
        objectives = sum(1 for g in self._store.goals.list(owner_user_id)
                         for _ in self._store.objectives.list_for_goal(g["goal_id"]))
        overview = ProgressOverview(
            objectives_total=objectives,
            objectives_engaged=len(masteries),
            mastered=sum(1 for m in masteries if m.get("state") == "MASTERED"),
            near_mastery=sum(1 for m in masteries
                             if m.get("state") == "NEAR_MASTERY"),
            regressed=sum(1 for m in masteries if m.get("state") == "REGRESSED"),
            due_reviews=len(self.due_reviews(owner_user_id)),
            study_minutes_total=sum(
                int(p.get("study_minutes") or 0)
                for p in self._store.progress.range(owner_user_id, "0000-01-01",
                                                    self._today())),
            practice_count=sum(
                int(p.get("practice_count") or 0)
                for p in self._store.progress.range(owner_user_id, "0000-01-01",
                                                    self._today())),
            assessment_score_avg=self._avg_assessment(owner_user_id),
            misconceptions=len(self.misconceptions.for_owner(owner_user_id)),
        )
        return overview

    def _avg_assessment(self, owner_user_id: int) -> Optional[float]:
        rows = self._store.assessments.list(owner_user_id, limit=1000)
        correct = sum(1 for r in rows if r.get("correct"))
        if not rows:
            return None
        return round(100.0 * correct / len(rows), 2)

    def daily_plan(self, owner_user_id: int, *,
                   day: Optional[str] = None,
                   time_minutes: Optional[int] = None) -> Dict[str, Any]:
        return _as_dict(self.planner.plan_day(owner_user_id, day, time_minutes))

    def weekly_review(self, owner_user_id: int,
                      days: int = 7) -> Dict[str, Any]:
        result = self.review.weekly(owner_user_id, days)
        self._store.events.log(owner_user_id, "weekly_review", None, None,
                               {"period_end": result.period_end})
        return _as_dict(result)

    def coach_today(self, owner_user_id: int) -> Dict[str, Any]:
        return _as_dict(self.coach.today(owner_user_id))

    # ------------------------------------------------------------------
    # Book-to-course
    # ------------------------------------------------------------------
    def book_course(self, owner_user_id: int, source_id: int, *,
                    title: Optional[str] = None,
                    mode: str = "READ") -> Optional[Dict[str, Any]]:
        return self.books.create_course(
            owner_user_id=owner_user_id, source_id=source_id, title=title,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # Privacy
    # ------------------------------------------------------------------
    def export_learning(self, owner_user_id: int) -> Dict[str, Any]:
        return {
            "goals": self._store.goals.list(owner_user_id),
            "objectives": [
                dict(o) for g in self._store.goals.list(owner_user_id)
                for o in self._store.objectives.list_for_goal(g["goal_id"])
            ],
            "curricula": [
                dict(c) for g in self._store.goals.list(owner_user_id)
                for c in self._store.curricula.list_for_goal(owner_user_id, g["goal_id"])
            ],
            "mastery": self._store.mastery.list_for_owner(owner_user_id),
            "reviews": self._store.reviews.list(owner_user_id),
            "assessments": self._store.assessments.list(owner_user_id, limit=500),
            "misconceptions": [
                dict(m) for m in self.misconceptions.for_owner(owner_user_id)
            ],
            "plans": self._store.plans.recent(owner_user_id, 30),
            "progress": [
                dict(p) for p in self._store.progress.range(owner_user_id,
                                                            "0000-01-01",
                                                            "9999-12-31")
            ],
        }

    def delete_learning(self, owner_user_id: int) -> bool:
        return self._store.clear_owner(owner_user_id)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _as_dict(value) -> Dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {k: _as_dict(v) for k, v in value.__dict__.items()}
    return value


def _lesson_dict(lesson: Lesson) -> Dict[str, Any]:
    return {
        "lesson_id": lesson.lesson_id,
        "objective_id": lesson.objective_id,
        "objective_title": lesson.objective_title,
        "grounded": lesson.grounded,
        "content": lesson.content,
        "sources": lesson.sources,
    }


def _session_dict(session) -> Dict[str, Any]:
    return _as_dict(session)


def _session_row(data: Dict[str, Any]):
    from app.learning.models import LearningSession
    known = {f for f in LearningSession.__dataclass_fields__}
    return LearningSession(**{k: v for k, v in data.items() if k in known})


def _origin_of(lesson: Lesson) -> str:
    content = lesson.content or {}
    if content.get("schema_version") and any(
            isinstance(content.get(k), str) and len(content[k]) > 200
            for k in ("explanation", "recap") if content.get(k)):
        return "llm"
    return "template"


def _question_from_dict(data: Dict[str, Any]):
    from app.learning.models import AssessmentQuestion
    known = {f for f in AssessmentQuestion.__dataclass_fields__}
    return AssessmentQuestion(**{k: v for k, v in data.items() if k in known})


def _base_tokens(text: str) -> set:
    import re
    return set(re.findall(r"[\w\u0600-\u06FF]{2,}", (text or "").lower()))


__all__ = ["AdaptiveLearningEngine"]