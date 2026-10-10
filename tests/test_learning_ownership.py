"""Cross-owner reference rejection for learning relationships.

Owner A (1) creates parent rows (goals, objectives, curricula, lessons, sessions,
knowledge sources). Owner B (2) then tries to create or update child rows that
point at A's parent IDs while B's own child rows keep B's owner_user_id. Every
such write must be rejected at the repository (database boundary) level, and
raw SQL must show no row owned by B that references A's parent.

Each relation also has a same-owner success case so the validation cannot
simply block everything.

The bodies below are shared with tests/test_learning_ownership_pg.py, which runs
them against PostgreSQL 16 by replacing only the ``manager`` fixture.
"""

from __future__ import annotations

import json

import pytest

import database
from app.knowledge.repositories import KnowledgeStore
from app.learning.repositories import LearningStore

OWNER_A = 1
OWNER_B = 2


def _raw(manager, sql: str, params=()):
    with manager._lock:
        conn = manager._get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


def _count(manager, table: str, owner: int) -> int:
    return _raw(manager, f"SELECT COUNT(*) FROM {table} WHERE owner_user_id = ?",
                (owner,))[0][0]


def _cross_refs(manager, table: str, column: str, owner: int, parent_id) -> int:
    return _raw(manager,
                f"SELECT COUNT(*) FROM {table} WHERE owner_user_id = ? AND {column} = ?",
                (owner, parent_id))[0][0]


@pytest.fixture()
def manager():
    return database.db._manager


@pytest.fixture()
def knowledge(manager):
    return KnowledgeStore(manager)


@pytest.fixture()
def learning(manager):
    return LearningStore(manager)


@pytest.fixture()
def parents(learning, knowledge):
    """A builds a full parent chain: goal, objective, curriculum, lesson, session, source."""
    goal = learning.goals.create(owner_user_id=OWNER_A, kind="skill", title="A goal")
    objective = learning.objectives.create(
        owner_user_id=OWNER_A, goal_id=goal, title="A objective")
    other_objective = learning.objectives.create(
        owner_user_id=OWNER_A, goal_id=goal, title="A second objective")
    curriculum = learning.curricula.create(
        owner_user_id=OWNER_A, goal_id=goal, title="A curriculum")
    session = learning.sessions.create(
        owner_user_id=OWNER_A, goal_id=goal, objective_id=objective,
        curriculum_id=curriculum, mode="READ")
    lesson = learning.lessons.create(
        owner_user_id=OWNER_A, curriculum_id=curriculum, objective_id=objective,
        source_id=None, grounded=False, content={"text": "A lesson"})
    source = knowledge.sources.create(
        owner_user_id=OWNER_A, source_type="txt", title="A source",
        file_name="a.txt", file_format="txt", mime_type="text/plain",
        file_size_bytes=1, checksum="sum-learning-a", storage_key="key/learning-a",
        original_ref=None)
    for value in (goal, objective, other_objective, curriculum, session, lesson, source):
        assert value
    return {"goal": goal, "objective": objective, "other_objective": other_objective,
            "curriculum": curriculum, "session": session, "lesson": lesson,
            "source": source}


@pytest.fixture()
def b_parents(learning):
    """B owns its own parents so that foreign-plus-own mixes can be tested."""
    goal = learning.goals.create(owner_user_id=OWNER_B, kind="skill", title="B goal")
    objective = learning.objectives.create(
        owner_user_id=OWNER_B, goal_id=goal, title="B objective")
    curriculum = learning.curricula.create(
        owner_user_id=OWNER_B, goal_id=goal, title="B curriculum")
    assert goal and objective and curriculum
    return {"goal": goal, "objective": objective, "curriculum": curriculum}


# --- Goals -----------------------------------------------------------------

def test_goal_parent_from_other_owner_is_rejected(learning, manager, parents):
    before = _count(manager, "learning_goals", OWNER_B)
    result = learning.goals.create(owner_user_id=OWNER_B, kind="skill", title="child",
                                   parent_goal_id=parents["goal"])
    assert result is None
    assert _count(manager, "learning_goals", OWNER_B) == before
    assert _cross_refs(manager, "learning_goals", "parent_goal_id",
                       OWNER_B, parents["goal"]) == 0


def test_goal_parent_same_owner_succeeds(learning, manager, parents):
    child = learning.goals.create(owner_user_id=OWNER_A, kind="skill", title="child",
                                  parent_goal_id=parents["goal"])
    assert child
    assert _cross_refs(manager, "learning_goals", "parent_goal_id",
                       OWNER_A, parents["goal"]) == 1


# --- Objectives ------------------------------------------------------------

def test_objective_goal_from_other_owner_is_rejected(learning, manager, parents):
    before = _count(manager, "learning_objectives", OWNER_B)
    result = learning.objectives.create(owner_user_id=OWNER_B, goal_id=parents["goal"],
                                        title="B objective on A goal")
    assert result is None
    assert _count(manager, "learning_objectives", OWNER_B) == before
    assert _cross_refs(manager, "learning_objectives", "goal_id",
                       OWNER_B, parents["goal"]) == 0


def test_objective_goal_same_owner_succeeds(learning, manager, parents):
    assert learning.objectives.create(owner_user_id=OWNER_A,
                                      goal_id=parents["goal"], title="ok")


def test_objective_prerequisite_from_other_owner_is_rejected(learning, manager,
                                                             parents, b_parents):
    before = _count(manager, "learning_objectives", OWNER_B)
    result = learning.objectives.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"], title="needs A",
        prerequisites=[parents["objective"]])
    assert result is None
    assert _count(manager, "learning_objectives", OWNER_B) == before
    assert _raw(manager, "SELECT COUNT(*) FROM learning_objectives "
                "WHERE owner_user_id = ? AND prerequisites_json LIKE ?",
                (OWNER_B, f"%{parents['objective']}%"))[0][0] == 0


def test_objective_mixed_prerequisites_reject_whole_write(learning, manager,
                                                          parents, b_parents):
    """One foreign ID among valid own IDs must reject the whole batch."""
    result = learning.objectives.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"], title="mixed",
        prerequisites=[b_parents["objective"], parents["objective"]])
    assert result is None
    assert _raw(manager, "SELECT COUNT(*) FROM learning_objectives "
                "WHERE owner_user_id = ? AND title = ?", (OWNER_B, "mixed"))[0][0] == 0


def test_objective_nonexistent_prerequisite_is_rejected(learning, manager, b_parents):
    result = learning.objectives.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"], title="ghost",
        prerequisites=[999999])
    assert result is None


def test_objective_prerequisite_same_owner_succeeds(learning, parents):
    assert learning.objectives.create(
        owner_user_id=OWNER_A, goal_id=parents["goal"], title="needs other",
        prerequisites=[parents["objective"]])


def test_set_prerequisites_with_other_owner_objective_is_rejected(
        learning, manager, parents, b_parents):
    result = learning.objectives.set_prerequisites(
        OWNER_B, b_parents["objective"], [parents["objective"]])
    assert result is False
    assert _raw(manager, "SELECT prerequisites_json FROM learning_objectives "
                "WHERE objective_id = ? AND owner_user_id = ?",
                (b_parents["objective"], OWNER_B))[0][0] in ("[]", None)


def test_set_prerequisites_same_owner_succeeds(learning, manager, parents):
    assert learning.objectives.set_prerequisites(
        OWNER_A, parents["objective"], [parents["other_objective"]]) is True


# --- Curricula -------------------------------------------------------------

def test_curriculum_goal_from_other_owner_is_rejected(learning, manager, parents):
    before = _count(manager, "learning_curricula", OWNER_B)
    result = learning.curricula.create(owner_user_id=OWNER_B, goal_id=parents["goal"],
                                       title="B curriculum on A goal")
    assert result is None
    assert _count(manager, "learning_curricula", OWNER_B) == before
    assert _cross_refs(manager, "learning_curricula", "goal_id",
                       OWNER_B, parents["goal"]) == 0


def test_curriculum_goal_same_owner_succeeds(learning, parents):
    assert learning.curricula.create(owner_user_id=OWNER_A,
                                     goal_id=parents["goal"], title="ok")


def test_curriculum_modules_with_other_owner_objective_are_rejected(
        learning, manager, parents, b_parents):
    modules = [{"module_index": 1, "title": "m", "objective_ids": [parents["objective"]]}]
    result = learning.curricula.update_modules(
        OWNER_B, b_parents["curriculum"], modules)
    assert result is False
    assert _raw(manager, "SELECT modules_json FROM learning_curricula "
                "WHERE curriculum_id = ?", (b_parents["curriculum"],))[0][0] in ("[]", None)


def test_curriculum_modules_same_owner_succeeds(learning, parents):
    modules = [{"module_index": 1, "title": "m",
                "objective_ids": [parents["objective"]]}]
    assert learning.curricula.update_modules(OWNER_A, parents["curriculum"], modules)


# --- Lessons ---------------------------------------------------------------

def _lesson_kwargs(owner, **overrides):
    kwargs = dict(owner_user_id=owner, curriculum_id=None, objective_id=None,
                  source_id=None, grounded=False, content={"text": "x"})
    kwargs.update(overrides)
    return kwargs


def test_lesson_curriculum_from_other_owner_is_rejected(learning, manager,
                                                        parents, b_parents):
    result = learning.lessons.create(**_lesson_kwargs(
        OWNER_B, curriculum_id=parents["curriculum"],
        objective_id=b_parents["objective"]))
    assert result is None
    assert _cross_refs(manager, "learning_lessons", "curriculum_id",
                       OWNER_B, parents["curriculum"]) == 0


def test_lesson_objective_from_other_owner_is_rejected(learning, manager,
                                                       parents, b_parents):
    result = learning.lessons.create(**_lesson_kwargs(
        OWNER_B, curriculum_id=b_parents["curriculum"],
        objective_id=parents["objective"]))
    assert result is None
    assert _cross_refs(manager, "learning_lessons", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_lesson_source_from_other_owner_is_rejected(learning, manager,
                                                    parents, b_parents):
    before = _count(manager, "learning_lessons", OWNER_B)
    result = learning.lessons.create(**_lesson_kwargs(
        OWNER_B, curriculum_id=b_parents["curriculum"],
        objective_id=b_parents["objective"], source_id=parents["source"]))
    assert result is None
    assert _count(manager, "learning_lessons", OWNER_B) == before
    assert _cross_refs(manager, "learning_lessons", "source_id",
                       OWNER_B, parents["source"]) == 0


def test_lesson_same_owner_parents_succeed(learning, parents):
    # The fixture already holds a lesson for (curriculum, objective); use the
    # second objective so this success case does not hit the unique key.
    assert learning.lessons.create(**_lesson_kwargs(
        OWNER_A, curriculum_id=parents["curriculum"],
        objective_id=parents["other_objective"], source_id=parents["source"]))


# --- Sessions --------------------------------------------------------------

def test_session_goal_from_other_owner_is_rejected(learning, manager, parents, b_parents):
    result = learning.sessions.create(
        owner_user_id=OWNER_B, goal_id=parents["goal"], objective_id=None,
        curriculum_id=None, mode="READ")
    assert result is None
    assert _cross_refs(manager, "learning_sessions", "goal_id",
                       OWNER_B, parents["goal"]) == 0


def test_session_objective_from_other_owner_is_rejected(learning, manager, parents, b_parents):
    result = learning.sessions.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"],
        objective_id=parents["objective"], curriculum_id=None, mode="READ")
    assert result is None
    assert _cross_refs(manager, "learning_sessions", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_session_curriculum_from_other_owner_is_rejected(learning, manager, parents, b_parents):
    result = learning.sessions.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"], objective_id=None,
        curriculum_id=parents["curriculum"], mode="READ")
    assert result is None
    assert _cross_refs(manager, "learning_sessions", "curriculum_id",
                       OWNER_B, parents["curriculum"]) == 0


def test_session_same_owner_parents_succeed(learning, parents):
    assert learning.sessions.create(
        owner_user_id=OWNER_A, goal_id=parents["goal"],
        objective_id=parents["objective"], curriculum_id=parents["curriculum"],
        mode="READ")


def test_session_set_step_with_other_owner_lesson_is_rejected(learning, manager,
                                                              parents, b_parents):
    session = learning.sessions.create(
        owner_user_id=OWNER_B, goal_id=b_parents["goal"], objective_id=None,
        curriculum_id=None, mode="READ")
    assert session
    result = learning.sessions.set_step(OWNER_B, session, "active", "lesson",
                                        lesson_id=parents["lesson"])
    assert result is False
    assert _cross_refs(manager, "learning_sessions", "lesson_id",
                       OWNER_B, parents["lesson"]) == 0


def test_session_set_step_same_owner_lesson_succeeds(learning, manager, parents):
    assert learning.sessions.set_step(OWNER_A, parents["session"], "active", "lesson",
                                      lesson_id=parents["lesson"]) is True
    assert _cross_refs(manager, "learning_sessions", "lesson_id",
                       OWNER_A, parents["lesson"]) == 1


# --- Mastery, reviews, assessments, misconceptions, events -----------------

def test_mastery_save_with_other_owner_objective_is_rejected(learning, manager,
                                                             parents):
    result = learning.mastery.save(OWNER_B, parents["objective"], "learning",
                                   50.0, 1, [], [])
    assert result is False
    assert _cross_refs(manager, "learning_mastery", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_mastery_save_same_owner_succeeds(learning, manager, parents):
    assert learning.mastery.save(OWNER_A, parents["objective"], "learning",
                                 50.0, 1, [], []) is True


def test_review_create_with_other_owner_objective_is_rejected(learning, manager, parents):
    result = learning.reviews.create(owner_user_id=OWNER_B,
                                     objective_id=parents["objective"],
                                     concept="c", next_review="2026-10-11")
    assert result is None
    assert _cross_refs(manager, "learning_reviews", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_review_create_same_owner_succeeds(learning, parents):
    assert learning.reviews.create(owner_user_id=OWNER_A,
                                   objective_id=parents["objective"],
                                   concept="c", next_review="2026-10-11")


def test_assessment_with_other_owner_objective_is_rejected(learning, manager, parents):
    result = learning.assessments.create(
        owner_user_id=OWNER_B, session_id=None, objective_id=parents["objective"],
        kind="quiz", concept="c", prompt="p")
    assert result is None
    assert _cross_refs(manager, "learning_assessments", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_assessment_with_other_owner_session_is_rejected(learning, manager,
                                                         parents, b_parents):
    result = learning.assessments.create(
        owner_user_id=OWNER_B, session_id=parents["session"],
        objective_id=b_parents["objective"], kind="quiz", concept="c", prompt="p")
    assert result is None
    assert _cross_refs(manager, "learning_assessments", "session_id",
                       OWNER_B, parents["session"]) == 0


def test_assessment_same_owner_succeeds(learning, parents):
    assert learning.assessments.create(
        owner_user_id=OWNER_A, session_id=parents["session"],
        objective_id=parents["objective"], kind="quiz", concept="c", prompt="p")


def test_misconception_with_other_owner_objective_is_rejected(learning, manager, parents):
    result = learning.misconceptions.record(OWNER_B, parents["objective"],
                                            "pattern", "evidence")
    assert result is False
    assert _cross_refs(manager, "learning_misconceptions", "objective_id",
                       OWNER_B, parents["objective"]) == 0


def test_misconception_same_owner_succeeds(learning, manager, parents):
    assert learning.misconceptions.record(OWNER_A, parents["objective"],
                                          "pattern", "evidence") is True


def test_event_with_other_owner_objective_or_session_is_rejected(learning, manager,
                                                                 parents, b_parents):
    assert learning.events.log(OWNER_B, "viewed", objective_id=parents["objective"]) is False
    assert learning.events.log(OWNER_B, "viewed", session_id=parents["session"]) is False
    assert _cross_refs(manager, "learning_events", "objective_id",
                       OWNER_B, parents["objective"]) == 0
    assert _cross_refs(manager, "learning_events", "session_id",
                       OWNER_B, parents["session"]) == 0


def test_event_same_owner_succeeds(learning, manager, parents):
    assert learning.events.log(OWNER_A, "viewed", objective_id=parents["objective"],
                               session_id=parents["session"], detail={"k": 1}) is True
    row = _raw(manager, "SELECT detail_json FROM learning_events "
               "WHERE owner_user_id = ? AND objective_id = ?",
               (OWNER_A, parents["objective"]))[0][0]
    assert json.loads(row) == {"k": 1}
