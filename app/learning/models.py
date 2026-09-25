"""AUSTRO AI - Adaptive learning domain (DTOs + constants, Phase E).

Read paths return plain dicts (faithful to the SQLite row) for rendering;
these typed DTOs are used for the deterministic algorithms (mastery, spaced
repetition, curriculum) and for validated structured output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Mastery model.
MASTERY_STATES = ("NEW", "LEARNING", "PRACTICING", "NEAR_MASTERY",
                  "MASTERED", "REGRESSED")
_DIAGNOSTIC_STATUSES = ("unknown", "weak", "partially_known", "known")
DIAGNOSTIC_LEVELS = ("beginner", "intermediate", "advanced")

# Evidence kinds that count as distinct sources of mastery evidence.
EVIDENCE_KINDS = ("assessment", "review", "practice", "quiz")

# Chemistry of a lesson (spec order in section 9).
LESSON_BLOCKS = (
    "title", "objective", "prerequisites", "explanation", "example",
    "guided_practice", "independent_practice", "quick_check", "recap",
    "next_step",
)

ASSESSMENT_KINDS = (
    "multiple_choice", "true_false", "short_answer", "explanation", "practical",
)

# Deterministic SM-0 style schedule (days). Will evolve later.
REVIEW_INTERVALS = (1, 2, 4, 7, 15, 30)

SESSION_STATES = ("STARTED", "LESSON", "PRACTICE", "ASSESS", "REVIEW",
                  "COMPLETED")
_SESSION_STEPS = ("STARTED", "LESSON", "PRACTICE", "ASSESS", "REVIEW",
                  "COMPLETED")

CURRICULUM_MODES = ("READ", "DEEP", "FAST", "EXAM", "PRACTICAL")

GOAL_KINDS = ("vision", "long_term", "milestone", "weekly_objective",
              "daily_action")

OBJECTIVE_STATUSES = ("planned", "in_progress", "completed", "deferred")


@dataclass
class Objective:
    objective_id: int = 0
    owner_user_id: int = 0
    goal_id: int = 0
    title: str = ""
    description: str = ""
    difficulty: str = "medium"
    prerequisites: List[int] = field(default_factory=list)
    status: str = "planned"
    mastery_state: str = "NEW"
    mastery_score: float = 0.0


@dataclass
class CurriculumModule:
    module_index: int = 1
    title: str = ""
    objective_ids: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"module_index": self.module_index, "title": self.title,
                "objective_ids": list(self.objective_ids)}


@dataclass
class Curriculum:
    curriculum_id: int = 0
    owner_user_id: int = 0
    goal_id: int = 0
    title: str = ""
    mode: str = "READ"
    modules: List[CurriculumModule] = field(default_factory=list)

    def ordered_objective_ids(self) -> List[int]:
        out: List[int] = []
        seen = set()
        for module in sorted(self.modules, key=lambda m: m.module_index):
            for obj_id in module.objective_ids:
                if obj_id not in seen:
                    seen.add(obj_id)
                    out.append(obj_id)
        return out


@dataclass
class Lesson:
    lesson_id: Optional[int] = None
    owner_user_id: int = 0
    curriculum_id: int = 0
    objective_id: int = 0
    objective_title: str = ""
    source_id: Optional[int] = None
    grounded: bool = False
    content: Dict[str, Any] = field(default_factory=dict)
    # Citations/provenance: list of {source_id, section_title, page}.
    sources: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.content.get("title", "")

    def block(self, name: str) -> str:
        return str(self.content.get(name, ""))


@dataclass
class AssessmentQuestion:
    kind: str = "short_answer"
    concept: str = ""
    prompt: str = ""
    options: List[str] = field(default_factory=list)
    answer: str = ""
    keywords: List[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "concept": self.concept,
            "prompt": self.prompt,
            "options": list(self.options),
            "answer": self.answer,
            "keywords": list(self.keywords),
            "explanation": self.explanation,
        }


@dataclass
class AssessmentResult:
    objective_id: int
    score: float = 0.0
    correct_count: int = 0
    total: int = 0
    weak_concepts: List[str] = field(default_factory=list)
    feedback: str = ""
    next_action: str = ""
    mastery_state: str = ""


@dataclass
class ReviewItem:
    review_id: Optional[int] = None
    owner_user_id: int = 0
    objective_id: int = 0
    objective_title: str = ""
    concept: str = ""
    difficulty: str = "medium"
    last_reviewed: Optional[str] = None
    next_review: str = ""
    attempt_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    interval_days: int = 1
    ease: float = 2.5


@dataclass
class Misconception:
    misconception_id: Optional[int] = None
    owner_user_id: int = 0
    objective_id: int = 0
    objective_title: str = ""
    pattern: str = ""
    evidence: List[str] = field(default_factory=list)
    count: int = 1
    severity: str = "medium"
    acknowledged: bool = False


@dataclass
class LearningSession:
    session_id: Optional[int] = None
    owner_user_id: int = 0
    goal_id: Optional[int] = None
    objective_id: Optional[int] = None
    objective_title: str = ""
    curriculum_id: Optional[int] = None
    mode: str = "READ"
    state: str = "STARTED"
    step: str = "LESSON"
    lesson_id: Optional[int] = None
    started_at: str = ""
    ended_at: Optional[str] = None
    last_activity_at: str = ""
    result: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagnosticEntry:
    objective_id: int
    title: str
    status: str = "unknown"
    score: float = 0.0
    confidence: float = 0.0


@dataclass
class DiagnosticResult:
    goal_id: int
    overall_level: str = "beginner"
    entries: List[DiagnosticEntry] = field(default_factory=list)

    def statuses(self) -> Dict[int, str]:
        return {entry.objective_id: entry.status for entry in self.entries}


@dataclass
class PlanItem:
    kind: str = ""                 # review | lesson | practice | habit | task
    title: str = ""
    minutes: int = 0
    target_id: Optional[int] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "minutes": self.minutes,
                "target_id": self.target_id, "reason": self.reason}


@dataclass
class DailyPlan:
    owner_user_id: int = 0
    plan_date: str = ""
    items: List[PlanItem] = field(default_factory=list)
    source: str = "adaptive_planner"
    total_minutes: int = 0
    adjusted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"date": self.plan_date, "source": self.source,
                "adjusted": self.adjusted, "total_minutes": self.total_minutes,
                "items": [item.to_dict() for item in self.items]}


@dataclass
class WeeklyReviewResult:
    period_start: str = ""
    period_end: str = ""
    planned: int = 0
    completed: int = 0
    learning_sessions: int = 0
    mastery_deltas: Dict[str, str] = field(default_factory=dict)
    habit_consistency: float = 0.0
    weak_areas: List[str] = field(default_factory=list)
    improved: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)
    priorities: List[str] = field(default_factory=list)


@dataclass
class CoachFocus:
    focus: str = ""
    blocker: str = ""
    smallest_action: str = ""
    review_items: List[str] = field(default_factory=list)
    reason: str = ""
    llm: bool = False


@dataclass
class ProgressOverview:
    objectives_total: int = 0
    objectives_engaged: int = 0
    mastered: int = 0
    near_mastery: int = 0
    regressed: int = 0
    due_reviews: int = 0
    study_minutes_total: int = 0
    practice_count: int = 0
    assessment_score_avg: Optional[float] = None
    misconceptions: int = 0


# Provenance/origin constants for learning data.
ORIGIN_LLM = "llm"
ORIGIN_TEMPLATE = "template"
ORIGIN_BOOK = "book"


@dataclass
class Flashcard:
    id: Optional[int] = None
    front: str = ""
    back: str = ""
    concept: str = ""
    difficulty: str = "medium"


__all__ = ["MASTERY_STATES", "DIAGNOSTIC_LEVELS", "EVIDENCE_KINDS",
           "LESSON_BLOCKS", "ASSESSMENT_KINDS", "REVIEW_INTERVALS",
           "SESSION_STATES", "CURRICULUM_MODES", "GOAL_KINDS",
           "OBJECTIVE_STATUSES", "Objective", "CurriculumModule",
           "Curriculum", "Lesson", "AssessmentQuestion", "AssessmentResult",
           "ReviewItem", "Misconception", "LearningSession",
           "DiagnosticEntry", "DiagnosticResult", "PlanItem", "DailyPlan",
           "WeeklyReviewResult", "CoachFocus", "ProgressOverview",
           "ORIGIN_LLM", "ORIGIN_TEMPLATE", "ORIGIN_BOOK", "Flashcard"]