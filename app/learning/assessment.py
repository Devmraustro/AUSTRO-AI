"""AUSTRO AI - Assessment engine (Phase E).

Deterministic question generation (MCQ / true-false / short-answer /
explanation / practical) plus deterministic grading with keyword coverage.
Optional LLM generation is schema-validated and falls back to the built-in
bank. Every submitted attempt updates mastery, misconceptions, progress and
the audit log, and yields feedback + a recommended next action.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.learning.models import (
    ASSESSMENT_KINDS,
    AssessmentQuestion,
    AssessmentResult,
)
from app.learning.repositories import LearningStore
from app.learning.schemas import ASSESSMENT_FIELDS, valid_assessment

logger = logging.getLogger(__name__)

_CORRECT_THRESHOLD = 0.5
_MCQ_POOL = [
    "ما المقصود بـ {concept}؟",
    "أي مما يلي يصف {concept} بشكل صحيح؟",
    "ما الدور الرئيسي لـ {concept}؟",
]
_TF_POOL = [
    "{concept} جديد تماماً ولا علاقة له بالاستخدام العملي",
    "{concept} يُستخدم فقط من المحترفين",
    "فهم {concept} يتطلب إتقان أساسيات قبله",
]


def kw_tokens(text: str) -> set:
    return set(re.findall(r"[\w\u0600-\u06FF]+", (text or "").lower()))


class AssessmentEngine:
    def __init__(self, store: LearningStore, ai=None):
        self._store = store
        self._ai = ai

    # -- generation ---------------------------------------------------------
    def generate_questions(self, *, owner_user_id: int, objective_id: int,
                           count: int = 3, kinds: Optional[List[str]] = None,
                           lesson: Optional[Dict[str, Any]] = None,
                           use_llm: bool = False) -> List[AssessmentQuestion]:
        objective = self._store.objectives.get(objective_id)
        if objective is None:
            return []
        requested = kinds or ["short_answer", "multiple_choice", "true_false"]
        # Keep only supported kinds, add a default if none left.
        requested = [k for k in requested if k in ASSESSMENT_KINDS]
        if not requested:
            requested = ["short_answer"]

        # Prefer grounded material when a book-backed lesson exists.
        if use_llm and self._ai is not None and self._ai.provider.is_available:
            llm_questions = self._llm_questions(owner_user_id, objective, count)
            if llm_questions:
                return llm_questions

        bank = self._bank(objective, lesson)
        questions: List[AssessmentQuestion] = []
        for item in bank:
            if len(questions) >= count:
                break
            if item.kind not in requested:
                continue
            questions.append(item)
        return questions

    def _bank(self, objective: Dict[str, Any],
              lesson: Optional[Dict[str, Any]]) -> List[AssessmentQuestion]:
        concept = (objective.get("title") or "").strip() or "الهدف"
        questions: List[AssessmentQuestion] = []

        # Short answers from the lesson recap when available.
        if lesson and lesson.get("content"):
            content = lesson["content"]
            recap = (content.get("recap") or "").strip()
            keywords = list(kw_tokens(recap))[:5] or [concept]
            questions.append(AssessmentQuestion(
                kind="short_answer", concept=concept,
                prompt=f"لخّص بأسلوبك ما فهمته عن «{concept}».", keywords=keywords,
                explanation="قارن إجابتك بالنقاط الأساسية في الملخص.",
            ))
        else:
            questions.append(AssessmentQuestion(
                kind="short_answer", concept=concept,
                prompt=f"اشرح «{concept}» بأسلوبك الخاص.", keywords=[concept],
                explanation="على الأقل اذكر التعريف الرئيسي.",
            ))

        wrong_options = self._wrong_options(concept)
        questions.append(AssessmentQuestion(
            kind="multiple_choice", concept=concept,
            prompt=_MCQ_POOL[0].format(concept=concept),
            options=wrong_options + [f"{concept}: المفهوم الأساسي"],
            answer=f"{concept}: المفهوم الأساسي",
            explanation="الاختيار الصحيح هو تعريف {concept} بشكل مباشر.",
        ))

        statement = _TF_POOL[0].format(concept=concept)
        questions.append(AssessmentQuestion(
            kind="true_false", concept=concept, prompt=f"صح أم خطأ: {statement}",
            options=["صح", "خطأ"], answer="خطأ",
            explanation="البيان غير دقيق؛ {concept} مرتبط بالتطبيق العملي.",
        ))
        return questions

    @staticmethod
    def _wrong_options(concept: str) -> List[str]:
        return [
            f"{concept}: مفهوم متعلق بحلول الترفيه",
            f"{concept}: لا يمكن تعلمه", 
        ]

    def _llm_questions(self, owner_user_id: int, objective: Dict[str, Any],
                       count: int) -> List[AssessmentQuestion]:
        try:
            from app.domain.ai import AIRequest
            from app.config.prompts import GEMINI_STUDY_PROMPT
        except Exception:  # pragma: no cover - import is guarded
            return []
        request = AIRequest(
            capability="assessment_generation",
            prompt=(
                f"أنشئ {count} سؤال تقييم عن «{objective.get('title')}» بأصناف "
                "متعددة.\nأعد JSON بهذا الشكل: {\"questions\": [{\"kind\": "
                "\"multiple_choice|true_false|short_answer|explanation|practical\", "
                "\"concept\": \"\", \"prompt\": \"\", \"options\": [], "
                "\"answer\": \"\", \"keywords\": [], \"explanation\": \"\"}]}",
            ),
            system_prompt=GEMINI_STUDY_PROMPT,
            max_tokens=1200, temperature=0.2, user_id=owner_user_id,
            data={"objective": objective.get("title")},
        )
        try:
            import asyncio
            response = asyncio.new_event_loop().run_until_complete(
                self._ai.generate(request)
            )
            parsed = _parse_questions_json(response.text)
            return [AssessmentQuestion(**q) for q in parsed]
        except Exception as exc:  # pragma: no cover - fallback safety
            logger.warning(f"LLM assessment generation failed, using bank: {exc}")
            return []

    # -- grading ------------------------------------------------------------
    def grade(self, question: AssessmentQuestion, user_answer: str) -> Dict[str, Any]:
        user_answer = (user_answer or "").strip()
        if question.kind in ("multiple_choice", "true_false"):
            correct = _exact_match(user_answer, question.answer)
            score = 1.0 if correct else 0.0
            feedback = "إجابة صحيحة." if correct else \
                f"الإجابة الصحيحة: {question.answer}"
            return {"correct": correct, "score": score, "feedback": feedback}

        # Free-text kinds: keyword coverage.
        coverage = _keyword_coverage(question.keywords, user_answer)
        if question.kind == "short_answer":
            correct = coverage >= 0.5
            score = coverage
            feedback = _feedback_for(question, coverage)
        else:
            correct = coverage >= 0.5 and len(user_answer) >= 10
            score = coverage
            feedback = _feedback_for(question, coverage)
        return {"correct": correct, "score": round(score, 2),
                "feedback": feedback}

    def submit(self, *, owner_user_id: int, objective_id: int, questions,
               answers: Dict[int, str], session_id: Optional[int] = None,
               on: Optional[str] = None) -> AssessmentResult:
        """Grade a full attempt set, persist everything, update mastery."""
        results = [self.grade(q, answers.get(index, ""))
                   for index, q in enumerate(questions)]
        correct_count = sum(1 for r in results if r["correct"])
        total = len(results) or 1
        score = (correct_count / total) * 100.0
        weak_concepts: List[str] = []
        for index, question in enumerate(questions):
            graded = results[index]
            self._store.assessments.create(
                owner_user_id=owner_user_id, session_id=session_id,
                objective_id=objective_id, kind=question.kind,
                concept=question.concept, prompt=question.prompt,
                options=question.options, expected=question.answer,
                keywords=question.keywords,
                user_answer=answers.get(index, ""), correct=graded["correct"],
                score=graded["score"], feedback=graded["feedback"],
            )
            if not graded["correct"]:
                weak_concepts.append(question.concept or "عام")

        # Mastery: weighted by item correctness (assessment evidence).
        skill = self._mastery_skill(owner_user_id, objective_id)
        for index, question in enumerate(questions):
            graded = results[index]
            skill.record(bool(graded["correct"]), "assessment")

        self._persist_mastery(owner_user_id, objective_id, skill.series())
        on_date = on or _today()
        self._store.progress.record(
            owner_user_id, on_date,
            assessment_count=total,
            assessment_score_avg=round(score, 2) if score else None,
        )
        self._store.events.log(
            owner_user_id, "assessment_submitted", objective_id, session_id,
            {"score": round(score, 2), "questions": total},
        )

        result = AssessmentResult(
            objective_id=objective_id,
            score=round(score, 2),
            correct_count=correct_count,
            total=total,
            weak_concepts=list(dict.fromkeys(weak_concepts)),
            feedback=_band_feedback(score),
            next_action=_next_action(score, weak_concepts),
            mastery_state=skill.state,
        )
        return result

    # -- mastery helpers ----------------------------------------------------
    def _mastery_skill(self, owner_user_id: int, objective_id: int):
        from app.learning.mastery import MasterySkill
        record = self._store.mastery.get(owner_user_id, objective_id)
        if record is None:
            return MasterySkill()
        return MasterySkill(state=record.get("state", "NEW"),
                            score=record.get("score", 0.0),
                            evidence_count=record.get("evidence_count", 0),
                            recent=record.get("recent") or [],
                            kinds=record.get("kinds") or [])

    def _persist_mastery(self, owner_user_id: int, objective_id: int,
                         series: Dict[str, Any]) -> None:
        self._store.mastery.save(
            owner_user_id, objective_id,
            state=series["state"], score=series["score"],
            evidence_count=series["evidence_count"],
            recent=series["recent"], kinds=series["kinds"],
        )
        self._store.objectives.update_mastery(
            objective_id, series["state"], series["score"],
        )


def _exact_match(user: str, expected: str) -> bool:
    return _normalize(user) == _normalize(expected)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _keyword_coverage(keywords: List[str], answer: str) -> float:
    """Matched keywords / total keywords, in [0, 1].

    A keyword matches when it fully equals a token or is contained inside one,
    so Arabic derivational forms (e.g. مداخلة vs الاقتصاد) still count.
    """
    if not keywords:
        return 0.0
    answer_tokens = kw_tokens(answer)
    if not answer_tokens:
        return 0.0
    matched = sum(
        1 for kw in keywords
        if any(kw in token or token in kw for token in answer_tokens)
    )
    return min(1.0, matched / len(keywords))


def _feedback_for(question: AssessmentQuestion, coverage: float) -> str:
    if coverage >= 1.0:
        return "إجابة ممتازة، غطّيت النقاط الأساسية."
    if coverage >= 0.5:
        return "إجابة جيدة؛ أضف التفاصيل المفقودة: " + ", ".join(question.keywords[:3])
    return "إجابة ناقصة. راجع المفاهيم الأساسية للهدف مرة أخرى."


def _band_feedback(score: float) -> str:
    if score >= 90:
        return "🌟 أداء رائع! أنت تتقن هذا الهدف."
    if score >= 70:
        return "👍 أداء جيد جداً، تحتاج مراجعة خفيفة فقط."
    if score >= 50:
        return "📚 أداء متوسط. أعد مراجعة المفاهيم ثم أعد المحاولة."
    return "🧱 تحتاج أن تبدأ من الأساسيات لهذا الهدف."


def _next_action(score: float, weak_concepts: List[str]) -> str:
    if score >= 90:
        return "انتقل إلى الهدف التالي أو خذ مراجعة متباعدة."
    if score >= 70:
        return "مراجعة سريعة للمفاهيم الضعيفة ثم اختبار قصير."
    if weak_concepts:
        return f"أعد دراسة: {', '.join(weak_concepts[:2])} ثم أعد الاختبار."
    return "ابدأ الدرس من جديد بجزء الشرح."


def _parse_questions_json(text: str):
    from app.learning.schemas import extract_json, validate_schema
    data = extract_json(text)
    if not isinstance(data, dict) or "questions" not in data:
        return []
    parsed = []
    for item in data.get("questions") or []:
        if not isinstance(item, dict):
            continue
        if validate_schema(item, ASSESSMENT_FIELDS):
            continue
        if not valid_assessment(item):
            continue
        parsed.append(item)
    return parsed


def _today() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d")


__all__ = ["AssessmentEngine", "kw_tokens"]