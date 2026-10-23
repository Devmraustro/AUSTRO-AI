"""Regression tests for the post-PR#3 hardening pass.

Covers audit items deliberately left open by PR #3:
- C-2 habit logging per-day idempotency (repository + core v2 migration),
- C-3 RAG ``grounded`` reflecting evidential support, not chunk presence,
- C-5 learning validation enforced at the service layer.
"""

import sqlite3
import threading

import pytest

import database
from app.ai.gateway import AIGateway
from app.core.container import build_container
from app.knowledge.models import RetrievedChunk
from app.knowledge.rag import RAGService, _INSUFFICIENT_TEXT
from app.knowledge.retrieval import RetrievalResult


# --------------------------------------------------------------------------
# C-2: habit idempotency
# --------------------------------------------------------------------------

def _habit(user=42):
    db = database.db
    assert db.create_user(user, "t", "Tester")
    habit_id = db.habits.create(user, "قراءة", "10 صفحات")
    return db, habit_id


def _habit_row(db, habit_id):
    cursor = db.habits._connection().cursor()
    cursor.execute("SELECT current_streak, total_completions FROM habits WHERE habit_id=?",
                   (habit_id,))
    return cursor.fetchone()


def _log_count(db, habit_id, date):
    cursor = db.habits._connection().cursor()
    cursor.execute("SELECT COUNT(*) FROM habit_logs WHERE habit_id=? AND date=?",
                   (habit_id, date))
    return cursor.fetchone()[0]


def test_habit_log_same_day_twice_is_idempotent():
    db, habit_id = _habit()
    assert db.habits.log(habit_id, 42, "2026-10-23", True)
    assert db.habits.log(habit_id, 42, "2026-10-23", True)  # retry / double tap
    assert _log_count(db, habit_id, "2026-10-23") == 1
    assert tuple(_habit_row(db, habit_id)) == (1, 1)


def test_habit_log_correction_adjusts_streak_once():
    db, habit_id = _habit(43)
    assert db.habits.log(habit_id, 43, "2026-10-23", True)
    assert db.habits.log(habit_id, 43, "2026-10-23", False)  # correction
    assert _log_count(db, habit_id, "2026-10-23") == 1
    assert tuple(_habit_row(db, habit_id)) == (0, 0)
    assert db.habits.log(habit_id, 43, "2026-10-23", True)  # corrected back
    assert tuple(_habit_row(db, habit_id)) == (1, 1)


def test_habit_log_concurrent_same_day_single_row():
    db, habit_id = _habit(44)
    results = []

    def worker():
        results.append(db.habits.log(habit_id, 44, "2026-10-23", True))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results) and len(results) == 8
    assert _log_count(db, habit_id, "2026-10-23") == 1
    assert tuple(_habit_row(db, habit_id)) == (1, 1)


def test_core_v2_migration_dedupes_preserves_nulls_and_reruns(tmp_path):
    from app.database.migrations import apply_migrations

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE habit_logs (log_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " habit_id INTEGER, user_id INTEGER, date DATE, completed BOOLEAN, note TEXT)")
    conn.execute("CREATE TABLE schema_migrations (schema_name TEXT PRIMARY KEY,"
                 " version INTEGER NOT NULL, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    # Pretend core v1 already applied so only v2 runs here.
    conn.execute("INSERT INTO schema_migrations VALUES ('core', 1, CURRENT_TIMESTAMP)")
    conn.executemany(
        "INSERT INTO habit_logs (habit_id, user_id, date, completed) VALUES (?,?,?,?)",
        [(1, 7, "2026-10-01", 1), (1, 7, "2026-10-01", 1), (1, 7, "2026-10-02", 1),
         (None, 7, "2026-10-01", 1), (2, 7, None, 1)],
    )
    conn.commit()
    apply_migrations(conn)
    apply_migrations(conn)  # repeated execution must be a no-op
    rows = conn.execute("SELECT habit_id, date FROM habit_logs ORDER BY log_id").fetchall()
    assert rows.count((1, "2026-10-01")) == 1
    assert (None, "2026-10-01") in rows and (2, None) in rows  # NULL rows preserved
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO habit_logs (habit_id, user_id, date, completed)"
                     " VALUES (1, 7, '2026-10-01', 1)")
    conn.close()


# --------------------------------------------------------------------------
# C-3: RAG grounding reflects evidential support
# --------------------------------------------------------------------------

class _FakeRetrieval:
    def __init__(self, chunks):
        self._chunks = chunks

    def retrieve(self, owner, question, top_k=None, collections=None):
        return RetrievalResult(query=question, chunks=self._chunks, event_id=1,
                               generator="fake")

    def as_citations(self, chunks):
        from app.knowledge.models import Citation
        return [Citation(source_title=c.title, source_id=c.source_id) for c in chunks]


class _FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, text):
        self._text = text

    @property
    def is_available(self):
        return True

    async def generate(self, request):
        return self._text


def _rag(chunks, answer_text):
    container = build_container()
    gateway = AIGateway(settings_=container.settings, provider=_FakeProvider(answer_text))
    return RAGService(retrieval=_FakeRetrieval(chunks), ai=gateway,
                      settings_=container.settings)


_CHUNK = RetrievedChunk(chunk_row_id=1, source_id=1, score=0.9, title="كتاب",
                        content="تنظيم الوقت مهارة تسمح بإنجاز الأهداف اليومية بكفاءة")


async def test_rag_grounded_when_answer_cites_evidence():
    rag = _rag([_CHUNK], "تنظيم الوقت مهارة لإنجاز الأهداف [1]")
    answer = await rag.answer(1, "ما تنظيم الوقت؟")
    assert answer.grounded is True and answer.citations


async def test_rag_not_grounded_for_unrelated_answer():
    rag = _rag([_CHUNK], "عاصمة فرنسا هي باريس وهي مدينة جميلة جداً للسياحة")
    answer = await rag.answer(1, "ما تنظيم الوقت؟")
    assert answer.grounded is False


async def test_rag_not_grounded_when_model_declares_insufficiency():
    rag = _rag([_CHUNK], _INSUFFICIENT_TEXT)
    answer = await rag.answer(1, "سؤال")
    assert answer.grounded is False


async def test_rag_blank_chunks_are_insufficient_without_ai_call():
    blank = RetrievedChunk(chunk_row_id=2, source_id=1, score=0.9, title="كتاب", content="   ")
    rag = _rag([blank], "أي إجابة مختلقة [1]")
    answer = await rag.answer(1, "سؤال")
    assert answer.grounded is False and answer.text == _INSUFFICIENT_TEXT


# --------------------------------------------------------------------------
# C-5: learning service-layer validation
# --------------------------------------------------------------------------

def _engine_with_objective():
    engine = build_container().learning_engine
    chain = engine.create_goal_chain(777, vision="v", long_term="البرمجة")
    objective_id = engine.add_objective(777, chain["long_term_goal_id"], title="المتغيرات")
    return engine, objective_id


def test_learning_submit_rejects_foreign_objective():
    engine, objective_id = _engine_with_objective()
    questions = engine.assessment(777, objective_id, count=1)
    with pytest.raises(ValueError):
        engine.submit_assessment(999, objective_id, questions=questions,
                                 answers={0: "x"})


def test_learning_submit_rejects_empty_and_forged():
    engine, objective_id = _engine_with_objective()
    with pytest.raises(ValueError):
        engine.submit_assessment(777, objective_id, questions=[], answers={})
    with pytest.raises(ValueError):
        engine.submit_assessment(777, objective_id,
                                 questions=[{"prompt": "س؟", "answer": "", "kind": "multiple_choice",
                                             "concept": "c", "options": [], "keywords": []}],
                                 answers={0: "x"})


def test_learning_submit_rejects_out_of_range_answers():
    engine, objective_id = _engine_with_objective()
    questions = engine.assessment(777, objective_id, count=1)
    with pytest.raises(ValueError):
        engine.submit_assessment(777, objective_id, questions=questions, answers={5: "x"})
