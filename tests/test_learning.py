"""Phase E - Adaptive coaching + universal learning engine tests.

Offline integration tests for the learning subsystem: curriculum
topological ordering (cycles tolerated), deterministic assessment grading,
mastery progression to MASTERED with mixed evidence, spaced-review wiring,
the adaptive planner, weekly/coach engines, book-to-course with a fabricated
knowledge source, privacy export/delete, the DI container wiring and the
Telegram handler registration. Everything is deterministic - no network.
"""

import pytest

from app.core.container import build_container
from app.learning.eval import run_all

OWNER = 9001


@pytest.fixture()
def engine():
    return build_container().learning_engine


@pytest.fixture()
def container():
    return build_container()


def _seed_goal(engine, titles=("المتغيرات", "الحلقات", "الدوال")):
    chain = engine.create_goal_chain(
        OWNER, vision="رؤيتي: تعلم البرمجة", long_term="البرمجة",
    )
    goal_id = chain["long_term_goal_id"]
    assert goal_id is not None
    objective_ids = []
    previous = None
    for title in titles:
        objective_id = engine.add_objective(
            OWNER, goal_id, title=title,
            prerequisites=[previous] if previous else None,
        )
        assert objective_id is not None
        objective_ids.append(objective_id)
        previous = objective_id
    curriculum = engine.build_curriculum(OWNER, goal_id, title="منهج البرمجة")
    return goal_id, objective_ids, curriculum["curriculum_id"]


def _start_lesson(engine, curriculum_id):
    outcome = engine.next_lesson(OWNER, curriculum_id)
    assert outcome is not None, "next_lesson returned None"
    assert outcome["objective"]["objective_id"]
    assert outcome["session"]["session_id"]
    return outcome


def _questions_for(engine, objective_id, session_id):
    questions = engine.quick_check(OWNER, session_id, objective_id)
    assert len(questions) == 2
    for q in questions:
        assert q.get("options"), f"question has no inline options: {q['kind']}"
    return questions


def _all_correct_answers(questions):
    """short_answer is keyword-graded (no reference answer); others opt/answer."""
    answers = {}
    for index, q in enumerate(questions):
        if q["kind"] == "short_answer":
            answers[index] = " ".join(q.get("keywords") or []) or "نعم"
        else:
            answers[index] = q["answer"]
    return answers


# ============================ DETERMINISM ============================


def test_container_wires_learning_engine(container):
    assert container.learning_engine is not None


def test_curriculum_respects_prerequisites_and_orders(engine):
    _, _, curriculum_id = _seed_goal(engine)
    curriculum = engine.curriculum(OWNER, curriculum_id)
    ordered = engine.curricula.ordered_objective_ids(curriculum)
    assert ordered == sorted(ordered), "topological order broken"
    assert len(ordered) == 3


def test_lesson_content_is_deterministic_and_versioned(engine):
    _, _, curriculum_id = _seed_goal(engine)
    first = engine.next_lesson(OWNER, curriculum_id)
    second = engine.next_lesson(OWNER, curriculum_id)
    assert first["lesson"]["lesson_id"] == second["lesson"]["lesson_id"]
    content = first["lesson"]["content"]
    assert content["schema_version"] == 1
    assert content["title"] and content["explanation"]


def test_mastery_progresses_to_mastered_with_mixed_evidence(engine):
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    objective_id = outcome["objective"]["objective_id"]
    session_id = outcome["session"]["session_id"]

    # Two all-correct assessments (3 questions each) -> NEAR_MASTERY.
    for _ in range(2):
        questions = engine.assessment(OWNER, objective_id, count=3)
        answers = _all_correct_answers(questions)
        result = engine.submit_assessment(
            OWNER, objective_id, questions=questions, answers=answers,
            session_id=session_id,
        )
        assert result["score"] == 100.0, (result["score"], questions)
        assert result["correct_count"] == result["total"]

    rows = engine._store.mastery.list_for_owner(OWNER)
    assert rows
    assert rows[0]["state"] == "NEAR_MASTERY"

    # A correct spaced review supplies the 2nd evidence kind -> MASTERED.
    review_id = engine._create_review(OWNER, objective_id, "مفهوم")
    engine.record_review(OWNER, review_id, correct=True)
    rows = engine._store.mastery.list_for_owner(OWNER)
    assert rows[0]["state"] == "MASTERED"

    overview = engine.progress_overview(OWNER)
    assert overview.mastered >= 1


def test_quick_check_grades_mcq_and_true_false_inline(engine):
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    objective_id = outcome["objective"]["objective_id"]
    session_id = outcome["session"]["session_id"]

    questions = _questions_for(engine, objective_id, session_id)
    answers = {i: q["answer"] for i, q in enumerate(questions)}
    graded = engine.submit_quick_check(
        OWNER, session_id, objective_id, answers, questions=questions,
    )
    assert graded["result"]["score"] == 100.0
    assert "result" in graded and "questions" in graded


def test_grading_is_keyword_deterministic(engine):
    """Free-text answers are graded by keyword coverage, not by LLM."""
    from app.learning.assessment import AssessmentEngine  # noqa: PLC0415
    from app.learning.models import AssessmentQuestion  # noqa: PLC0415

    grader = AssessmentEngine(engine._store)
    q = AssessmentQuestion(
        kind="short_answer", concept="المتغيرات",
        prompt="ما هي المتغيرات؟",
        keywords=["متغير", "قيمة", "خزن"],
        answer="",
    )
    partial = grader.grade(q, "المتغير يخزن القيمة")
    assert partial["correct"] is True
    wrong = grader.grade(q, "لا أعرف")
    assert wrong["correct"] is False


# ============================ SPACED REVIEW ============================


def test_weak_assessment_schedules_reviews_for_tomorrow(engine):
    _, objective_ids, _ = _seed_goal(engine)
    objective_id = objective_ids[0]
    questions = engine.assessment(OWNER, objective_id, count=2)
    answers = {}  # all unanswered -> wrong -> weak concepts
    engine.submit_assessment(OWNER, objective_id, questions=questions, answers=answers)

    reviews = engine._store.reviews.list(OWNER)
    assert reviews, "weak assessment should schedule a review"
    assert engine.due_reviews(OWNER) == []  # scheduled for tomorrow
    assert reviews[0]["next_review"] > engine._today()
    assert engine.record_review(OWNER, reviews[0]["review_id"], correct=True)
    updated = engine._store.reviews.list(OWNER)[0]
    assert updated["next_review"] > reviews[0]["next_review"]


# ============================ PLANNER + COACH + WEEKLY ============================


def test_daily_plan_has_lesson_item(engine):
    goal_id, _, _ = _seed_goal(engine)
    plan = engine.daily_plan(OWNER, time_minutes=60)
    assert isinstance(plan, dict)
    assert plan["total_minutes"] > 0
    assert any(item["kind"] == "lesson" for item in plan["items"])


def test_coach_today_returns_focus_keys(engine):
    _seed_goal(engine)
    focus = engine.coach_today(OWNER)
    for key in ("focus", "blocker", "smallest_action", "reason"):
        assert key in focus


def test_weekly_review_returns_summary_keys(engine):
    _seed_goal(engine)
    week = engine.weekly_review(OWNER)
    for key in ("period_start", "period_end", "planned", "completed",
                "learning_sessions", "improved", "failed", "weak_areas",
                "changes"):
        assert key in week


# ============================ BOOK-TO-COURSE ============================


async def test_book_course_creates_curriculum_from_source(engine):
    services = build_container()
    upload = services.knowledge.register_upload(
        owner_user_id=OWNER,
        file_name="مبادئ البرمجة.txt",
        data="المتغيرات تخزن القيم. الحلقات تكرر التنفيذ. الدوال تعيد الاستخدام.".encode("utf-8"),
        mime_type="text/plain",
    )
    result = await services.knowledge.process_source(OWNER, upload["source_id"])
    assert result is not None

    course = services.learning_engine.book_course(
        OWNER, upload["source_id"], title="دورة مبادئ البرمجة",
    )
    assert course is not None
    assert course["curriculum_id"]
    assert len(course["objectives"]) >= 1

    curriculum = engine.curriculum(OWNER, course["curriculum_id"])
    assert curriculum is not None
    outcome = engine.next_lesson(OWNER, course["curriculum_id"], grounded=True)
    assert outcome is not None


# ============================ PRIVACY ============================


def test_privacy_export_and_delete(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    questions = engine.assessment(OWNER, outcome["objective"]["objective_id"], count=2)
    answers = {i: q["answer"] for i, q in enumerate(questions)}
    engine.submit_assessment(
        OWNER, outcome["objective"]["objective_id"],
        questions=questions, answers=answers,
        session_id=outcome["session"]["session_id"],
    )

    exported = engine.export_learning(OWNER)
    assert "goals" in exported
    assert "curricula" in exported
    assert "mastery" in exported

    assert engine.delete_learning(OWNER) is True
    assert engine.goals(OWNER) == []
    assert engine._store.mastery.list_for_owner(OWNER) == []


# ============================ GOLDEN EVAL + HANDLERS ============================


def test_golden_eval_still_passes():
    results = run_all()
    assert results["all_passed"] is True
    assert results["checks"]["mastery"] is True
    assert results["checks"]["schedule"] is True
    assert results["checks"]["kinds"] is True


def test_learning_handlers_registered():
    from handlers import get_learning_handlers, get_menu_handlers  # noqa: PLC0415
    from telegram.ext import ConversationHandler  # noqa: PLC0415
    assert isinstance(get_learning_handlers(), ConversationHandler)
    handlers = get_menu_handlers()
    patterns = []
    for h in handlers:
        pat = getattr(h, "pattern", None)
        if pat is not None:
            patterns.append(pat.pattern if hasattr(pat, "pattern") else str(pat))
    for token in ("menu_learn", "menu_learn_quiz", "menu_learn_review",
                  "learn_quiz_done", "learn_newgoal"):
        assert any(token in p for p in patterns), token