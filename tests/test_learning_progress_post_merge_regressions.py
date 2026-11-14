"""Post-merge learning-progress regressions found in the PR #16 audit.

All three behaviours fail on the merged code:

* a non-assessment event erased the day's ``assessment_score_avg`` because
  the repository wrote ``None`` for every event that did not carry a score;
* a 0% assessment stored ``NULL`` instead of a measured ``0.0``;
* ``record()`` read the day's row outside the write lock, so two concurrent
  evidence events lost one increment;
* replaying the same review callback (double tap / unconfirmed poll offset)
  counted the answer twice in mastery and in the review counter.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.container import build_container
from app.learning.engine import AdaptiveLearningEngine
from app.learning.repositories import LearningStore

OWNER = 9501


@pytest.fixture()
def container():
    return build_container()


@pytest.fixture()
def store(container) -> LearningStore:
    return LearningStore(container.db._manager)


def _seed_objective(engine: AdaptiveLearningEngine) -> tuple[int, int]:
    chain = engine.create_goal_chain(
        OWNER, vision="رؤية", long_term="هدف طويل",
    )
    goal_id = chain["long_term_goal_id"]
    objective_id = engine.add_objective(OWNER, goal_id, title="موضوع")
    assert goal_id is not None and objective_id is not None
    return goal_id, objective_id


# ---------------------------------------------------------------------------
# assessment_score_avg
# ---------------------------------------------------------------------------


def test_other_evidence_does_not_erase_the_assessment_average(store):
    day = "2026-05-01"
    assert store.progress.record(
        OWNER, day, assessment_count=4, assessment_score_avg=75.5
    )
    assert store.progress.record(OWNER, day, review_count=1)
    row = store.progress.get(OWNER, day)
    assert row["assessment_score_avg"] == 75.5
    assert row["assessment_count"] == 4
    assert row["review_count"] == 1


def test_zero_percent_assessment_is_stored_as_a_measured_zero(container):
    engine = container.learning_engine
    _, objective_id = _seed_objective(engine)
    questions = engine.assessment(OWNER, objective_id, count=2)
    result = engine.submit_assessment(
        OWNER, objective_id, questions=questions, answers={},
    )
    assert result["score"] == 0.0
    row = container.learning_engine._store.progress.get(OWNER, engine._today())
    assert row["assessment_score_avg"] == 0.0, (
        "a measured 0% must not be indistinguishable from 'no assessment'"
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_evidence_events_do_not_lose_increments(store, monkeypatch):
    day = "2026-05-02"
    real_get = type(store.progress).get

    def slow_get(self, *args, **kwargs):
        value = real_get(self, *args, **kwargs)
        # On the merged code record() called get() and then released the lock;
        # this delay makes the lost-update window deterministic. The atomic
        # implementation never calls get() at all.
        time.sleep(0.01)
        return value

    monkeypatch.setattr(type(store.progress), "get", slow_get)

    errors = []

    def worker():
        try:
            for _ in range(10):
                assert store.progress.record(OWNER, day, study_minutes=1)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    row = store.progress.get(OWNER, day)
    assert row["study_minutes"] == 40, (
        f"lost updates: expected 40, got {row['study_minutes']}"
    )


# ---------------------------------------------------------------------------
# Telegram review callback replay
# ---------------------------------------------------------------------------


def test_replayed_review_grading_is_idempotent(container):
    engine = container.learning_engine
    _, objective_id = _seed_objective(engine)
    review_id = engine._create_review(OWNER, objective_id, "مفهوم")
    assert review_id is not None

    store = engine._store
    item = store.reviews.get(OWNER, objective_id, "مفهوم")
    item["next_review"] = engine._today()  # make it due now
    assert store.reviews.update(OWNER, objective_id, "مفهوم", item)

    first = engine.record_review(OWNER, review_id, correct=True)
    assert first is not None
    mastery = store.mastery.get(OWNER, objective_id)
    progress = store.progress.get(OWNER, engine._today())
    assert mastery["evidence_count"] == 1
    assert progress["review_count"] == 1

    # The same callback delivered a second time (double tap or replay).
    second = engine.record_review(OWNER, review_id, correct=True)
    assert second is not None
    assert store.mastery.get(OWNER, objective_id)["evidence_count"] == 1
    assert store.progress.get(OWNER, engine._today())["review_count"] == 1
    assert store.reviews.get(OWNER, objective_id, "مفهوم")["attempt_count"] == 1


def test_upsert_handles_null_counters_without_losing_increments(store):
    """Exercise the real repository upsert against a legacy nullable schema.

    Current migrations declare the counters NOT NULL, but older installations
    may contain a compatible table where those columns are nullable. Recreate
    that legacy shape on the fixture's own connection, then invoke the actual
    LearningProgressRepository.record() method.
    """
    day = "2026-05-03"
    conn = store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute("DROP TABLE learning_progress")
    cursor.execute(
        "CREATE TABLE learning_progress ("
        "progress_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "owner_user_id INTEGER NOT NULL, "
        "record_date TEXT NOT NULL, "
        "study_minutes INTEGER, "
        "practice_count INTEGER, "
        "review_count INTEGER, "
        "assessment_count INTEGER, "
        "assessment_score_avg REAL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE (owner_user_id, record_date))"
    )
    cursor.execute(
        "INSERT INTO learning_progress "
        "(owner_user_id, record_date, study_minutes, practice_count, "
        "review_count, assessment_count, assessment_score_avg) "
        "VALUES (?, ?, NULL, NULL, NULL, NULL, NULL)",
        (OWNER, day),
    )
    conn.commit()

    assert store.progress.record(
        OWNER, day, study_minutes=15, practice_count=2, review_count=1
    )
    stored = store.progress.get(OWNER, day)
    assert stored["study_minutes"] == 15
    assert stored["practice_count"] == 2
    assert stored["review_count"] == 1
    assert stored["assessment_count"] == 0
    assert stored["assessment_score_avg"] is None

    # A subsequent assessment records a measured zero without losing earlier
    # counters; a later non-assessment event must preserve that measured value.
    assert store.progress.record(
        OWNER, day, study_minutes=5, assessment_count=1,
        assessment_score_avg=0.0,
    )
    assert store.progress.record(OWNER, day, review_count=1)
    stored = store.progress.get(OWNER, day)
    assert stored["study_minutes"] == 20
    assert stored["practice_count"] == 2
    assert stored["review_count"] == 2
    assert stored["assessment_count"] == 1
    assert stored["assessment_score_avg"] == 0.0
