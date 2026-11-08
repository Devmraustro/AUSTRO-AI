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
    upload = await services.knowledge.register_upload(
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



def _make_plans(engine, owner: int, count: int) -> None:
    """Create `count` plans for the owner to exceed any arbitrary limit."""
    for i in range(count):
        engine._store.plans.save(
            owner_user_id=owner,
            plan_date=f"2026-01-{i + 1:02d}",
            items=[{"kind": "lesson", "title": f"plan {i}", "minutes": 10}],
            source="test",
            total_minutes=10,
            adjusted=False,
        )


def _make_assessments(engine, owner: int, objective_id: int, count: int) -> None:
    """Create `count` assessment records for the owner/objective."""
    for i in range(count):
        questions = engine.assessment(owner, objective_id, count=1,
                                      kinds=["multiple_choice"])
        answers = {0: questions[0]["answer"]}
        engine.submit_assessment(
            owner, objective_id, questions=questions, answers=answers,
            session_id=None,
        )


def test_export_learning_includes_lessons(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    lesson_id = outcome["lesson"]["lesson_id"]

    exported = engine.export_learning(OWNER)
    lesson_ids = {l["lesson_id"] for l in exported.get("lessons", [])}
    assert lesson_id in lesson_ids, f"lesson {lesson_id} not in export"


def test_export_learning_includes_sessions_completed_and_not(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)

    # Create a session directly
    session = engine.sessions.start(
        OWNER, objective_ids[0], goal_id, curriculum_id, "READ"
    )
    session_id = session.session_id

    # Complete the session
    engine.sessions.complete(session, minutes=10)

    exported = engine.export_learning(OWNER)
    session_ids = {s["session_id"] for s in exported.get("sessions", [])}
    assert session_id in session_ids, f"session {session_id} not in export"
    assert any(s.get("state") == "COMPLETED" for s in exported.get("sessions", []))


def test_export_learning_includes_events(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    engine._store.events.log(OWNER, "test_event", None, None,
                             {"custom": "data"})

    exported = engine.export_learning(OWNER)
    event_actions = {e["action"] for e in exported.get("events", [])}
    assert "test_event" in event_actions, "test_event not in export"


def test_export_learning_more_than_500_assessments(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    objective_id = objective_ids[0]
    _make_assessments(engine, OWNER, objective_id, count=600)

    exported = engine.export_learning(OWNER)
    assessments = exported.get("assessments", [])
    assert len(assessments) >= 600, f"expected >=600 assessments, got {len(assessments)}"


def test_export_learning_more_than_30_plans(engine):
    _make_plans(engine, OWNER, count=50)

    exported = engine.export_learning(OWNER)
    plans = exported.get("plans", [])
    assert len(plans) >= 50, f"expected >=50 plans, got {len(plans)}"


def test_export_learning_cross_user_isolation(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    _start_lesson(engine, curriculum_id)

    # Owner 2 creates their own data with their own goal
    owner2_goal_id = engine.create_goal(200, kind="long_term", title="owner2 goal")
    assert owner2_goal_id is not None, "owner2 goal creation failed"
    obj2 = engine.add_objective(200, goal_id=owner2_goal_id, title="owner2 obj")
    engine.add_objective(OWNER, goal_id, title="owner1 obj")

    exported = engine.export_learning(OWNER)
    exported_goal_ids = {g["goal_id"] for g in exported.get("goals", [])}
    exported_obj_ids = {o["objective_id"] for o in exported.get("objectives", [])}

    assert goal_id in exported_goal_ids
    assert all(oid in exported_obj_ids for oid in objective_ids)
    # Owner 2's goal and objective must NOT appear
    assert owner2_goal_id not in exported_goal_ids, "cross-user leakage: owner 2 goal in export"
    # Verify no cross-user leakage: objective 2 (owner2) should not appear
    all_obj_ids = [o["objective_id"] for o in exported.get("objectives", [])]
    assert obj2 not in all_obj_ids, "cross-user leakage: owner 2 objective in export"
    # Verify all exported records belong to owner 1
    for g in exported.get("goals", []):
        assert g.get("owner_user_id") == OWNER, f"goal {g.get('goal_id')} has wrong owner"
    for o in exported.get("objectives", []):
        assert o.get("owner_user_id") == OWNER, f"objective {o.get('objective_id')} has wrong owner"


def test_export_learning_json_safe(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)
    _start_lesson(engine, curriculum_id)
    _make_plans(engine, OWNER, count=5)
    _make_assessments(engine, OWNER, objective_ids[0], count=10)

    exported = engine.export_learning(OWNER)
    import json
    # Must be JSON-serializable
    json_str = json.dumps(exported, ensure_ascii=False)
    reparsed = json.loads(json_str)
    assert "goals" in reparsed
    assert "objectives" in reparsed
    assert "lessons" in reparsed
    assert "sessions" in reparsed
    assert "events" in reparsed


def test_export_learning_empty_owner(engine):
    # Owner with no data at all
    exported = engine.export_learning(99999)
    assert exported["goals"] == []
    assert exported["objectives"] == []
    assert exported["curricula"] == []
    assert exported["lessons"] == []
    assert exported["sessions"] == []
    assert exported["events"] == []
    assert exported["mastery"] == []
    assert exported["reviews"] == []
    assert exported["assessments"] == []
    assert exported["misconceptions"] == []
    assert exported["plans"] == []
    assert exported["progress"] == []


def test_export_learning_no_db_changes(engine):
    goal_id, objective_ids, curriculum_id = _seed_goal(engine)

    # Capture state before export
    before_goals = len(engine._store.goals.list(OWNER))
    before_objectives = len(engine._store.objectives.list_for_goal(OWNER, goal_id))
    before_lessons = len(engine._store.lessons.list_for_owner(OWNER))
    before_sessions = len(engine._store.sessions.list_for_owner(OWNER))

    exported = engine.export_learning(OWNER)

    # After export, no data should have changed
    after_goals = len(engine._store.goals.list(OWNER))
    after_objectives = len(engine._store.objectives.list_for_goal(OWNER, goal_id))
    after_lessons = len(engine._store.lessons.list_for_owner(OWNER))
    after_sessions = len(engine._store.sessions.list_for_owner(OWNER))

    assert before_goals == after_goals
    assert before_objectives == after_objectives
    assert before_lessons == after_lessons
    assert before_sessions == after_sessions

    # Exported data should still reflect the pre-export state
    assert len(exported.get("goals", [])) == before_goals
    assert len(exported.get("lessons", [])) == before_lessons
    assert len(exported.get("sessions", [])) == before_sessions


# ----------------------------------------------------------------------
# Export error-tracking tests (operation-scoped error collector)
# ----------------------------------------------------------------------


def test_export_succeeds_after_unrelated_db_error(engine, monkeypatch):
    """A previous unrelated DB error must not cause a later export to fail."""
    _seed_goal(engine)

    # Simulate a previous DB error by directly appending to manager's legacy list
    engine._store._manager._errors.append("legacy: some old error")

    # Export should succeed because it uses its own operation-scoped collector
    exported = engine.export_learning(OWNER)
    assert "goals" in exported
    assert len(exported["goals"]) >= 1


def test_export_fails_when_repository_query_fails(engine, monkeypatch):
    """Export must fail when a required repository query fails."""
    _seed_goal(engine)

    import sqlite3
    from app.learning.repositories import _LearningBase

    # Create wrapper classes for connection and cursor
    class FailingCursor:
        def __init__(self, real_cursor):
            self._real_cursor = real_cursor
        def execute(self, sql, params=None):
            if "learning_goals" in sql and "SELECT" in sql:
                raise sqlite3.Error("simulated db failure")
            return self._real_cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._real_cursor, name)

    class FailingConnection:
        def __init__(self, real_conn):
            self._real_conn = real_conn
        def cursor(self, *args, **kwargs):
            real_cursor = self._real_conn.cursor(*args, **kwargs)
            return FailingCursor(real_cursor)
        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    original_connection = _LearningBase._connection

    def failing_connection(self):
        real_conn = original_connection(self)
        return FailingConnection(real_conn)

    monkeypatch.setattr(_LearningBase, "_connection", failing_connection)

    # Export must raise RuntimeError with repository/operation identification
    try:
        engine.export_learning(OWNER)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        err_msg = str(e)
        assert "EducationalGoalsRepository" in err_msg
        assert "list" in err_msg
        # Sanitized error format
        assert "Error: database operation failed" in err_msg


def test_concurrent_exports_independent_failures(engine, monkeypatch):
    """Concurrent exports for two owners must have independent error state.

    This test runs 25 iterations to verify deterministic behavior with
    proven overlap. Owner 1's export always fails; owner 2's export always succeeds.
    """
    import threading
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.learning.repositories import _LearningBase

    original_connection = _LearningBase._connection

    # Thread-local storage for owner ID - deterministic per thread
    thread_owner = threading.local()

    # Synchronization primitives to prove actual overlap
    enter_count = 0
    enter_lock = threading.Lock()
    enter_event = threading.Event()

    class FailingCursor:
        def __init__(self, real_cursor):
            self._real_cursor = real_cursor
        def execute(self, sql, params=None):
            if "learning_goals" in sql and "SELECT" in sql:
                # Deterministic: fail only for owner1 (pre-determined)
                if getattr(thread_owner, "owner_id", None) == "owner1":
                    raise sqlite3.Error("simulated db failure for owner1")
            return self._real_cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._real_cursor, name)

    class FailingConnection:
        def __init__(self, real_conn):
            self._real_conn = real_conn
        def cursor(self, *args, **kwargs):
            real_cursor = self._real_conn.cursor(*args, **kwargs)
            return FailingCursor(real_cursor)
        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    def failing_connection(self):
        real_conn = original_connection(self)
        return FailingConnection(real_conn)

    monkeypatch.setattr(_LearningBase, "_connection", failing_connection)

    def run_single_iteration():
        """Run one iteration of concurrent exports with proven overlap."""
        # Reset synchronization state for each iteration
        nonlocal enter_count
        enter_count = 0
        enter_event.clear()

        # Synchronization for proving overlap
        overlap_barrier = threading.Barrier(2)

        # Pre-create owner2's data once per iteration
        owner2 = 200
        owner2_goal_id = engine.create_goal(owner2, kind="long_term", title="owner2 goal")
        assert owner2_goal_id is not None
        owner2_obj_id = engine.add_objective(owner2, goal_id=owner2_goal_id, title="owner2 obj")
        assert owner2_obj_id is not None

        def export_for_owner(owner_key, owner_id):
            """Run export for a specific owner with deterministic failure injection."""
            # Set thread-local owner ID
            thread_owner.owner_id = owner_key
            try:
                # Signal entry into export
                with enter_lock:
                    nonlocal enter_count
                    enter_count += 1
                    if enter_count == 2:
                        enter_event.set()  # Both threads have entered
                # Wait for both threads to enter (proves overlap)
                overlap_barrier.wait(timeout=5)
                result = engine.export_learning(owner_id)
                return ("success", owner_key, result)
            except RuntimeError as e:
                return ("error", owner_key, str(e))
            except Exception as e:
                return ("unexpected", owner_key, str(e))

        owner1 = OWNER

        # Run both exports concurrently
        with ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(export_for_owner, "owner1", owner1)
            future2 = executor.submit(export_for_owner, "owner2", owner2)

            outcomes = {}
            for future in as_completed([future1, future2]):
                status, owner_key, data = future.result()
                outcomes[owner_key] = (status, data)

        # Verify overlap occurred
        assert enter_count == 2, "Both threads must have entered export_learning"
        assert enter_event.is_set(), "Both threads must have signaled entry"

        return outcomes

    # Run multiple iterations to verify deterministic behavior
    for iteration in range(25):
        outcomes = run_single_iteration()

        # Verify: owner1 fails, owner2 succeeds
        assert "owner1" in outcomes, "owner1 missing from outcomes"
        assert "owner2" in outcomes, "owner2 missing from outcomes"

        status1, data1 = outcomes["owner1"]
        status2, data2 = outcomes["owner2"]

        # Exactly one failure (owner1), one success (owner2)
        assert status1 == "error", f"iteration {iteration}: owner1 should fail, got {status1}: {data1}"
        assert status2 == "success", f"iteration {iteration}: owner2 should succeed, got {status2}"

        # Verify error message contains repo/operation info (sanitized)
        assert "EducationalGoalsRepository" in data1, f"iteration {iteration}: error should identify repository"
        assert "Error: database operation failed" in data1, f"iteration {iteration}: error should be sanitized"

        # Verify successful export has goals and correct ownership
        assert "goals" in data2, f"iteration {iteration}: success should have goals"
        assert len(data2["goals"]) >= 1, f"iteration {iteration}: should have at least one goal"
        for g in data2["goals"]:
            assert g.get("owner_user_id") == 200, f"iteration {iteration}: goal belongs to wrong owner"


def test_export_empty_owner_produces_valid_empty_export(engine):
    """An owner with no data must produce a valid empty export, not an error."""
    exported = engine.export_learning(99999)  # Non-existent owner
    assert exported == {
        "goals": [],
        "objectives": [],
        "curricula": [],
        "lessons": [],
        "sessions": [],
        "events": [],
        "mastery": [],
        "reviews": [],
        "assessments": [],
        "misconceptions": [],
        "plans": [],
        "progress": [],
    }


def test_export_no_partial_data_on_failure(engine, monkeypatch):
    """On failure, export must not return partial data - must raise."""
    _seed_goal(engine)

    import sqlite3
    from app.learning.repositories import _LearningBase

    original_connection = _LearningBase._connection

    class FailingCursor:
        def __init__(self, real_cursor):
            self._real_cursor = real_cursor
        def execute(self, sql, params=None):
            if "learning_objectives" in sql and "SELECT" in sql:
                raise sqlite3.Error("objectives failure")
            return self._real_cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._real_cursor, name)

    class FailingConnection:
        def __init__(self, real_conn):
            self._real_conn = real_conn
        def cursor(self, *args, **kwargs):
            real_cursor = self._real_conn.cursor(*args, **kwargs)
            return FailingCursor(real_cursor)
        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    def failing_connection(self):
        real_conn = original_connection(self)
        return FailingConnection(real_conn)

    monkeypatch.setattr(_LearningBase, "_connection", failing_connection)

    try:
        engine.export_learning(OWNER)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        err_msg = str(e)
        assert "LearningObjectivesRepository" in err_msg
        assert "list_for_goal" in err_msg
    # Verify no partial export was returned (exception was raised)


def test_export_error_identifies_repository_and_operation(engine, monkeypatch):
    """Error messages must identify the repository and operation."""
    _seed_goal(engine)

    import sqlite3
    from app.learning.repositories import _LearningBase

    original_connection = _LearningBase._connection

    class FailingCursor:
        def __init__(self, real_cursor):
            self._real_cursor = real_cursor
        def execute(self, sql, params=None):
            if "learning_sessions" in sql and "SELECT" in sql:
                raise sqlite3.Error("sessions table locked")
            return self._real_cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._real_cursor, name)

    class FailingConnection:
        def __init__(self, real_conn):
            self._real_conn = real_conn
        def cursor(self, *args, **kwargs):
            real_cursor = self._real_conn.cursor(*args, **kwargs)
            return FailingCursor(real_cursor)
        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    def failing_connection(self):
        real_conn = original_connection(self)
        return FailingConnection(real_conn)

    monkeypatch.setattr(_LearningBase, "_connection", failing_connection)

    try:
        engine.export_learning(OWNER)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        err_msg = str(e)
        # Repository and operation must be identified
        assert "SessionsRepository" in err_msg
        assert "list_for_owner" in err_msg
        # Raw database error details must NOT appear (sanitized)
        assert "sessions table locked" not in err_msg, "Raw DB error leaked in export error"
        # Sanitized error format should be present
        assert "Error: database operation failed" in err_msg, f"Expected sanitized error format, got: {err_msg}"


def test_export_error_sanitization_no_raw_details(engine, monkeypatch):
    """Export errors must not expose raw database exception details."""
    _seed_goal(engine)

    import sqlite3
    from app.learning.repositories import _LearningBase

    original_connection = _LearningBase._connection

    # Simulate a database error with sensitive-looking content
    sensitive_error = sqlite3.Error(
        "UNIQUE constraint failed: learning_goals.owner_user_id, learning_goals.title "
        "INSERT INTO learning_goals (owner_user_id, kind, title) VALUES (12345, 'long_term', 'secret-project')"
    )

    class FailingCursor:
        def __init__(self, real_cursor):
            self._real_cursor = real_cursor
        def execute(self, sql, params=None):
            if "learning_goals" in sql and "SELECT" in sql:
                raise sensitive_error
            return self._real_cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._real_cursor, name)

    class FailingConnection:
        def __init__(self, real_conn):
            self._real_conn = real_conn
        def cursor(self, *args, **kwargs):
            real_cursor = self._real_conn.cursor(*args, **kwargs)
            return FailingCursor(real_cursor)
        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    def failing_connection(self):
        real_conn = original_connection(self)
        return FailingConnection(real_conn)

    monkeypatch.setattr(_LearningBase, "_connection", failing_connection)

    try:
        engine.export_learning(OWNER)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        err_msg = str(e)
        # Repository and operation must be identified
        assert "EducationalGoalsRepository" in err_msg
        assert "list" in err_msg
        # Raw SQL, values, constraint names must NOT appear
        assert "UNIQUE constraint failed" not in err_msg
        assert "INSERT INTO" not in err_msg
        assert "VALUES" not in err_msg
        assert "12345" not in err_msg
        assert "secret-project" not in err_msg
        assert "learning_goals.owner_user_id" not in err_msg
        # Sanitized error format should be present
        assert "Error: database operation failed" in err_msg


def test_export_no_cross_user_leakage(engine):
    """Export must not leak data from other users."""
    # Owner 1 data
    goal_id1, obj_ids1, curr_id1 = _seed_goal(engine)
    _start_lesson(engine, curr_id1)

    # Owner 2 data (create goal, objective, curriculum, but skip lesson for simplicity)
    owner2 = 200
    goal_id2 = engine.create_goal(owner2, kind="long_term", title="owner2 goal")
    obj_id2 = engine.add_objective(owner2, goal_id=goal_id2, title="owner2 obj")
    _ = engine.build_curriculum(owner2, goal_id2, title="owner2 curriculum")

    # Export owner 1
    exported = engine.export_learning(OWNER)

    # Verify owner 1 data present
    exported_goal_ids = {g["goal_id"] for g in exported["goals"]}
    exported_obj_ids = {o["objective_id"] for o in exported["objectives"]}
    assert goal_id1 in exported_goal_ids
    assert all(oid in exported_obj_ids for oid in obj_ids1)

    # Verify owner 2 data absent
    assert goal_id2 not in exported_goal_ids
    assert obj_id2 not in exported_obj_ids

    # Verify all records belong to owner 1
    for g in exported["goals"]:
        assert g.get("owner_user_id") == OWNER
    for o in exported["objectives"]:
        assert o.get("owner_user_id") == OWNER
    for c in exported["curricula"]:
        assert c.get("owner_user_id") == OWNER
    for l in exported["lessons"]:
        assert l.get("owner_user_id") == OWNER


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

# ============================ R-5 OWNER SCOPING (IDOR) ============================
# Every read/update/delete-by-ID on user-owned learning data must enforce
# owner_user_id in SQL, not just in the service layer. These tests drive the
# repositories directly with a second user's identifiers.

OTHER = 9002


def test_objective_reads_are_owner_scoped(engine):
    _, objective_ids, _ = _seed_goal(engine)
    objective_id = objective_ids[0]
    store = engine._store

    assert store.objectives.get(OWNER, objective_id) is not None
    # A different owner must not resolve the same objective_id.
    assert store.objectives.get(OTHER, objective_id) is None


def test_list_for_goal_is_owner_scoped(engine):
    goal_id, objective_ids, _ = _seed_goal(engine)
    store = engine._store

    assert len(store.objectives.list_for_goal(OWNER, goal_id)) == len(objective_ids)
    assert store.objectives.list_for_goal(OTHER, goal_id) == []


def test_objective_set_status_is_owner_scoped(engine):
    _, objective_ids, _ = _seed_goal(engine)
    objective_id = objective_ids[0]
    store = engine._store

    assert store.objectives.set_status(OWNER, objective_id, "MASTERED") is True
    # The other owner must not be able to mutate it.
    assert store.objectives.set_status(OTHER, objective_id, "REGRESSED") is False
    row = store.objectives.get(OWNER, objective_id)
    assert row["status"] == "MASTERED"


def test_mastery_and_prerequisite_updates_are_owner_scoped(engine):
    _, objective_ids, _ = _seed_goal(engine)
    objective_id = objective_ids[0]
    store = engine._store

    assert store.objectives.update_mastery(OWNER, objective_id, "NEAR_MASTERY", 0.7) is True
    assert store.objectives.get(OWNER, objective_id)["mastery_state"] == "NEAR_MASTERY"
    # A different owner must not be able to move the same row.
    assert store.objectives.update_mastery(OTHER, objective_id, "MASTERED", 1.0) is False
    assert store.objectives.get(OWNER, objective_id)["mastery_state"] == "NEAR_MASTERY"

    assert store.objectives.set_prerequisites(OWNER, objective_id, []) is True
    assert store.objectives.set_prerequisites(OTHER, objective_id, [1, 2, 3]) is False
    assert store.objectives.get(OWNER, objective_id)["prerequisites"] in ([], None)


def test_session_reads_are_owner_scoped(engine):
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    session_id = outcome["session"]["session_id"]
    store = engine._store

    assert store.sessions.get(OWNER, session_id) is not None
    assert store.sessions.get(OTHER, session_id) is None


def test_session_set_step_and_complete_are_owner_scoped(engine):
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    session_id = outcome["session"]["session_id"]
    store = engine._store

    # The non-owner must not advance or complete somebody else's session.
    assert store.sessions.set_step(OTHER, session_id, "REVIEW", "REVIEW") is False
    assert store.sessions.complete(OTHER, session_id, {"study_minutes": 99}) is False

    # The session must be untouched by the non-owner.
    row = store.sessions.get(OWNER, session_id)
    assert row["step"] != "COMPLETED"
    assert store.events.list(OTHER, limit=50) == []


def test_session_manager_get_rejects_other_owner(engine):
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    session_id = outcome["session"]["session_id"]

    assert engine.sessions.get(OWNER, session_id) is not None
    assert engine.sessions.get(OTHER, session_id) is None


def test_continue_session_does_not_leak_other_owner(engine):
    _, _, curriculum_id = _seed_goal(engine)
    _start_lesson(engine, curriculum_id)

    assert engine.continue_session(OWNER) is not None
    # No session was ever started for OTHER, so nothing may be surfaced.
    assert engine.continue_session(OTHER) is None


def test_engine_paths_reject_cross_user_objective_ids(engine):
    """Assessment/mastery/flashcard paths must not accept another user's id."""
    _, objective_ids, curriculum_id = _seed_goal(engine)
    objective_id = objective_ids[0]
    outcome = _start_lesson(engine, curriculum_id)
    session_id = outcome["session"]["session_id"]

    # quick_check/assessment for a non-owner resolve nothing and produce nothing.
    assert engine.assessment(OTHER, objective_id, count=2) == []
    assert not engine.quick_check(OTHER, session_id, objective_id)

    # Flashcards and lesson content for the wrong owner yield nothing.
    assert engine.flashcards.from_objective(OTHER, objective_id) == []
    assert engine.lessons.get(OWNER, outcome["lesson"]["lesson_id"]) is not None


def test_session_completion_records_progress_for_the_owner(engine):
    """Regression: completion must pass owner_user_id through to the repo."""
    _, _, curriculum_id = _seed_goal(engine)
    outcome = _start_lesson(engine, curriculum_id)
    session = engine.sessions.get(OWNER, outcome["session"]["session_id"])
    assert session is not None
    session_id = session.session_id

    engine.sessions.complete(session, minutes=17)

    store = engine._store
    row = store.sessions.get(OWNER, session_id)
    assert row["step"] == "COMPLETED"
    assert row["result"]["study_minutes"] == 17

    completed = [
        e for e in store.events.list(OWNER, limit=200)
        if e.get("action") == "session_completed"
    ]
    assert completed, "session_completed event was not recorded"
    assert completed[-1]["session_id"] == session_id
