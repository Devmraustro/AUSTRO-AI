"""Phase G.1 - PostgreSQL repository-layer test matrix (real PostgreSQL).

These tests run the SAME repository / store / service code paths as the SQLite
suite, but against a real PostgreSQL 16 server. They exist to prove the
dual-engine data-access port works end to end:

- ``?`` placeholders translated to psycopg2 ``%s``
- ``INSERT OR REPLACE`` / ``INSERT OR IGNORE`` translated to PostgreSQL upserts
- scalar ``MAX(a, b)`` translated to ``GREATEST(a, b)``
- ``cursor.lastrowid`` emulated via ``INSERT ... RETURNING <pk>``
- BOOLEAN columns and TRUE/FALSE literals
- Python-computed UTC date cutoffs (replacing SQLite ``date('now', ...)``)
- row / value parity (str timestamps, ints for booleans, dict + positional rows)

An environment guard skips the whole module only when PostgreSQL is genuinely
unreachable - never to hide a failure.
"""

import os
import threading

import pytest

psycopg2 = pytest.importorskip("psycopg2")

_PG_HOST = os.environ.get("DB_HOST", "127.0.0.1")
_PG_PORT = int(os.environ.get("DB_PORT", "5432"))
_PG_USER = os.environ.get("DB_USER", "austro")
_PG_PASSWORD = os.environ.get("DB_PASSWORD", "austro_test_pw")
_PG_TEST_DB = os.environ.get("DB_TEST_NAME", "austro_ai_pgtest")
_PG_ADMIN_USER = os.environ.get("DB_ADMIN_USER", "postgres")
_PG_ADMIN_PASSWORD = os.environ.get("DB_ADMIN_PASSWORD", _PG_PASSWORD)


def _connect(dbname):
    return psycopg2.connect(
        host=_PG_HOST,
        port=_PG_PORT,
        dbname=dbname,
        user=_PG_USER,
        password=_PG_PASSWORD,
        connect_timeout=5,
    )


@pytest.fixture(autouse=True)
def fresh_db():  # noqa: PT004 - deliberately shadows tests/conftest.py fixture
    """Neutralise the suite-wide SQLite ``fresh_db`` fixture for this module."""
    yield None


@pytest.fixture(scope="session")
def _pg_engine():
    """Create the dedicated test database (engine patching happens per test)."""
    try:
        admin = psycopg2.connect(
            host=_PG_HOST,
            port=_PG_PORT,
            dbname="postgres",
            user=_PG_ADMIN_USER,
            password=_PG_ADMIN_PASSWORD,
            connect_timeout=5,
        )
    except psycopg2.OperationalError as exc:  # pragma: no cover - env guard
        pytest.skip(f"PostgreSQL not reachable at {_PG_HOST}:{_PG_PORT}: {exc}")

    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{_PG_TEST_DB}" WITH (FORCE)')
    cur.execute(f'CREATE DATABASE "{_PG_TEST_DB}" OWNER "{_PG_USER}"')
    cur.close()
    admin.close()

    yield

    admin = psycopg2.connect(
        host=_PG_HOST,
        port=_PG_PORT,
        dbname="postgres",
        user=_PG_ADMIN_USER,
        password=_PG_ADMIN_PASSWORD,
        connect_timeout=5,
    )
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{_PG_TEST_DB}" WITH (FORCE)')
    cur.close()
    admin.close()


def _snapshot_settings(fields):
    from app.config.settings import settings

    return {field: getattr(settings, field) for field in fields}


def _restore_settings(fields, snapshot):
    from app.config.settings import settings

    for field, value in snapshot.items():
        object.__setattr__(settings, field, value)


def _reset_public_schema():
    conn = _connect(_PG_TEST_DB)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
    cur.execute("CREATE SCHEMA public")
    cur.execute(f'GRANT ALL ON SCHEMA public TO "{_PG_USER}"')
    cur.close()
    conn.close()


@pytest.fixture()
def pg_db(_pg_engine):
    """A fresh ``Database`` facade on a clean PostgreSQL schema.

    Patching is per-test so the shared suite defaults back to SQLite between
    tests (never mutate process-wide engine state for the whole session).
    """
    import app.database as database_module
    from app.config.settings import settings
    import app.database.connection as connection_module
    import app.database.migrations as migrations_module

    try:
        database_module.db._close_connection()
    except Exception:  # noqa: BLE001 - shared singleton may not be initialised
        pass

    fields = ("db_engine", "db_host", "db_port", "db_name", "db_user", "db_password")
    snapshot = _snapshot_settings(fields)
    old_connection_engine = connection_module._DB_ENGINE
    old_migrations_engine = migrations_module._DB_ENGINE
    for _field, _value in (
        ("db_engine", "postgresql"),
        ("db_host", _PG_HOST),
        ("db_port", _PG_PORT),
        ("db_name", _PG_TEST_DB),
        ("db_user", _PG_USER),
        ("db_password", _PG_PASSWORD),
    ):
        object.__setattr__(settings, _field, _value)
    connection_module._DB_ENGINE = "postgresql"
    migrations_module._DB_ENGINE = "postgresql"

    _reset_public_schema()

    from app.database.connection import DatabaseManager
    from app.database.repositories import Database

    manager = DatabaseManager()
    database = Database(manager)
    try:
        yield database
    finally:
        manager._close_connection()
        _restore_settings(fields, snapshot)
        connection_module._DB_ENGINE = old_connection_engine
        migrations_module._DB_ENGINE = old_migrations_engine


@pytest.fixture()
def pg_knowledge(pg_db):
    from app.knowledge.repositories import KnowledgeStore

    return KnowledgeStore(pg_db._manager)


@pytest.fixture()
def pg_memory(pg_db):
    from app.memory.repositories import MemoryStore

    return MemoryStore(pg_db._manager)


@pytest.fixture()
def pg_learning(pg_db):
    from app.learning.repositories import LearningStore

    return LearningStore(pg_db._manager)


@pytest.fixture()
def pg_container(pg_db, tmp_path):
    from app.config.settings import settings
    from app.core.container import build_container

    original_path = settings.knowledge_storage_path
    object.__setattr__(settings, "knowledge_storage_path", str(tmp_path / "knowledge"))
    try:
        yield build_container(db_instance=pg_db)
    finally:
        object.__setattr__(settings, "knowledge_storage_path", original_path)


# ============================ SCHEMA ============================


def test_migrations_are_idempotent_on_postgres(pg_db):
    pg_db.init_database()
    pg_db.init_database()

    conn = pg_db._get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    assert cur.fetchone()[0] >= 40

    cur.execute("SELECT version FROM schema_migrations WHERE schema_name = %s", ("knowledge",))
    assert cur.fetchone()[0] == 1


# ============================ CORE / LEGACY ============================


def test_user_upsert_and_json_profile(pg_db):
    assert pg_db.create_user(101, "alice", "Alice") is True
    assert pg_db.create_user(101, "alice2", "Alice2") is True  # OR REPLACE upsert

    user = pg_db.get_user(101)
    assert user["username"] == "alice2"
    assert isinstance(user["last_active"], str)

    assert pg_db.update_profile(101, goals=["g1"], current_skills=["s1"]) is True
    user = pg_db.get_user(101)
    assert user["goals"] == ["g1"]
    assert user["current_skills"] == ["s1"]


def test_goal_lastrowid_owner_isolation_and_completion(pg_db):
    pg_db.create_user(1, "u1", "U1")
    pg_db.create_user(2, "u2", "U2")

    goal_id = pg_db.create_goal(1, "Learn", "desc", "study", ["s1", "s2"], None)
    assert isinstance(goal_id, int) and goal_id > 0

    assert [g["goal_id"] for g in pg_db.get_goals(1)] == [goal_id]
    assert pg_db.get_goals(2) == []

    assert pg_db.update_goal_progress(goal_id, 100) is True
    assert pg_db.get_goals(1)[0]["status"] == "completed"


def test_habits_boolean_and_greatest_streak(pg_db):
    pg_db.create_user(1, "u1", "U1")
    habit_id = pg_db.create_habit(1, "read", "daily reading")
    assert isinstance(habit_id, int) and habit_id > 0

    assert pg_db.log_habit(habit_id, 1, "2026-01-01", True, "done") is True
    habit = pg_db.get_habits(1)[0]
    assert habit["current_streak"] == 1
    assert habit["longest_streak"] == 1  # scalar MAX -> GREATEST
    assert habit["total_completions"] == 1

    assert pg_db.log_habit(habit_id, 1, "2026-01-02", False) is True
    habit = pg_db.get_habits(1)[0]
    assert habit["current_streak"] == 0
    assert habit["longest_streak"] == 1


def test_daily_reviews_upsert_and_date_cutoff(pg_db):
    from datetime import datetime, timedelta

    pg_db.create_user(1, "u1", "U1")

    assert pg_db.save_daily_review(1, "2026-01-01", "a", "l", "o", "t", 4) is True
    reviews = pg_db.get_daily_reviews(1)
    assert len(reviews) == 1
    assert reviews[0]["mood"] == 4

    today = datetime.utcnow().date()
    recent_date = today.isoformat()
    old_date = (today - timedelta(days=90)).isoformat()

    assert pg_db.log_progress(1, recent_date, study_hours=2.0, tasks_completed=3) is True
    assert pg_db.log_progress(1, old_date, study_hours=5.0) is True

    recent = pg_db.get_progress(1, days=30)
    dates = {r["date"] for r in recent}
    assert recent_date in dates
    assert old_date not in dates
    assert next(r for r in recent if r["date"] == recent_date)["study_hours"] == 2.0


def test_dashboard_stats_on_postgres(pg_db):
    pg_db.create_user(1, "u1", "U1")
    pg_db.create_goal(1, "g", "d", "c", [], None)
    stats = pg_db.get_dashboard_stats(1)
    assert stats["total_goals"] == 1
    assert stats["weekly_study_hours"] == 0


# ============================ REMINDERS ============================


def test_reminders_boolean_filter_and_lastrowid(pg_db):
    pg_db.create_user(10, "u", "U")
    one_off = pg_db.create_reminder(10, "custom", "t", "m", "2026-10-01T08:00:00", False)
    recurring = pg_db.create_reminder(10, "custom", "t2", "m2", "2026-10-02T09:00:00", True)
    assert isinstance(one_off, int) and one_off > 0
    assert isinstance(recurring, int) and recurring > 0

    active = pg_db.get_active_reminders(10)
    assert {r["reminder_id"] for r in active} == {one_off, recurring}

    all_active = pg_db.get_all_active_reminders()
    assert {r["reminder_id"] for r in all_active} == {one_off, recurring}
    # is_active is a real BOOLEAN on PostgreSQL; Python sees the 0/1 parity value
    assert all(r["is_active"] in (0, 1, True, False) for r in all_active)

    conn = pg_db._get_connection()
    conn.cursor().execute("UPDATE reminders SET is_active = FALSE WHERE reminder_id = %s", (one_off,))
    conn.commit()
    assert {r["reminder_id"] for r in pg_db.get_all_active_reminders()} == {recurring}


# ============================ KNOWLEDGE ============================


def test_knowledge_source_document_chunk_ids_and_embeddings(pg_knowledge):
    from app.knowledge.models import EmbeddingRecord

    store = pg_knowledge
    source_id = store.sources.create(
        owner_user_id=1,
        source_type="book",
        title="Time Management",
        file_name="book.txt",
        file_format="txt",
        mime_type="text/plain",
        file_size_bytes=100,
        checksum="abc123",
        storage_key="k/abc",
        original_ref=None,
    )
    assert isinstance(source_id, int) and source_id > 0
    # idempotent by (owner, checksum)
    assert (
        store.sources.create(
            owner_user_id=1,
            source_type="book",
            title="Time Management",
            file_name="book.txt",
            file_format="txt",
            mime_type="text/plain",
            file_size_bytes=100,
            checksum="abc123",
            storage_key="k/abc",
            original_ref=None,
        )
        is None
    )

    document_id = store.documents.create(
        source_id=source_id, owner_user_id=1, title="Time Management",
        author=None, language="en", toc=[], total_chars=100, total_pages=1,
    )
    assert isinstance(document_id, int) and document_id > 0

    section_id = store.sections.create(
        document_id=document_id, owner_user_id=1, source_id=source_id,
        level=1, title="Chapter 1", order_index=0, start_char=0, end_char=50,
    )
    assert isinstance(section_id, int) and section_id > 0

    chunk_id = store.chunks.create(
        owner_user_id=1, source_id=source_id, document_id=document_id,
        section_id=section_id, chunk_key="c1", content="time management",
        content_hash="h1", token_count=2, char_count=15, page=None,
        order_index=0,
    )
    assert isinstance(chunk_id, int) and chunk_id > 0
    assert store.chunks.count_for_source(source_id) == 1

    record = EmbeddingRecord(
        owner_user_id=1, source_id=source_id, chunk_row_id=chunk_id,
        model="local", version="1", dimensions=3, vector=[0.1, 0.2, 0.3],
    )
    assert store.embeddings.save_many([record]) == 1
    assert store.embeddings.count_for_source(source_id, "local", "1") == 1

    # OR REPLACE on (chunk_row_id, model, version) must update in place
    updated = EmbeddingRecord(
        owner_user_id=1, source_id=source_id, chunk_row_id=chunk_id,
        model="local", version="1", dimensions=3, vector=[0.9, 0.8, 0.7],
    )
    assert store.embeddings.save_many([updated]) == 1
    assert store.embeddings.count_for_source(source_id, "local", "1") == 1

    event_id = store.events.log(
        owner_user_id=1, query="time", top_k=5, result_count=1,
        latency_ms=1.5, generator="local",
    )
    assert isinstance(event_id, int) and event_id > 0
    assert (
        store.events.add_citations(
            event_id,
            [{"chunk_row_id": chunk_id, "source_id": source_id, "title": "t",
              "section_title": "s", "page": None, "snippet": "x", "score": 0.5}],
        )
        == 1
    )

    # owner isolation
    assert store.sources.get(2, source_id) is None


def test_knowledge_collections_ignore_and_storage_verified(pg_knowledge):
    store = pg_knowledge
    source_id = store.sources.create(
        owner_user_id=1, source_type="book", title="B", file_name="b.txt",
        file_format="txt", mime_type="text/plain", file_size_bytes=1,
        checksum="c1", storage_key="k1", original_ref=None,
    )
    collection_id = store.collections.create(1, "favs", "my favourites")
    assert isinstance(collection_id, int) and collection_id > 0

    assert store.collections.add_source(1, collection_id, source_id) is True
    # INSERT OR IGNORE -> ON CONFLICT DO NOTHING: duplicate add must not raise
    assert store.collections.add_source(1, collection_id, source_id) is True
    assert store.collections.source_ids_for(1, [collection_id]) == [source_id]

    assert store.storage.register(
        owner_user_id=1, source_id=source_id, storage_key="k1",
        file_name="b.txt", file_format="txt", size_bytes=1, checksum="c1",
    ) is True
    assert store.storage.verified(source_id) is False
    assert store.storage.mark_verified(source_id) is True
    assert store.storage.verified(source_id) is True


async def test_knowledge_ingestion_end_to_end_on_postgres(pg_container):
    knowledge = pg_container.knowledge
    upload = knowledge.register_upload(
        owner_user_id=7,
        file_name="notes.txt",
        data=b"Time management is the key skill. Focus on one task at a time.",
        mime_type="text/plain",
    )
    result = await knowledge.process_source(7, upload["source_id"])
    assert result is not None
    assert result.source["source_id"] == upload["source_id"]
    assert result.chunk_count >= 1


# ============================ LEARNING ============================


def test_learning_goal_objective_session_assessment_ids(pg_learning):
    store = pg_learning
    goal_id = store.goals.create(owner_user_id=1, kind="long_term", title="Master time")
    assert isinstance(goal_id, int) and goal_id > 0
    objective_id = store.objectives.create(
        owner_user_id=1, goal_id=goal_id, title="Prioritisation"
    )
    assert isinstance(objective_id, int) and objective_id > 0
    session_id = store.sessions.create(
        owner_user_id=1, goal_id=goal_id, objective_id=objective_id,
        curriculum_id=None,
    )
    assert isinstance(session_id, int) and session_id > 0
    attempt_id = store.assessments.create(
        owner_user_id=1, session_id=session_id, objective_id=objective_id,
        kind="mcq", concept="prioritise", prompt="Pick one", options=["a", "b"],
        correct=True, score=1.0,
    )
    assert isinstance(attempt_id, int) and attempt_id > 0

    assert store.goals.get(2, goal_id) is None  # owner isolation


def test_learning_mastery_upsert_reviews_ignore_and_plans_replace(pg_learning):
    store = pg_learning
    goal_id = store.goals.create(owner_user_id=1, kind="long_term", title="G")
    objective_id = store.objectives.create(
        owner_user_id=1, goal_id=goal_id, title="O"
    )

    assert store.mastery.save(1, objective_id, "LEARNING", 0.4, 2, [1], ["mcq"]) is True
    assert store.mastery.save(1, objective_id, "MASTERED", 0.9, 5, [1, 2], ["mcq"]) is True
    mastery = {m["objective_id"]: m for m in store.mastery.list_for_owner(1)}
    assert mastery[objective_id]["state"] == "MASTERED"
    assert len(store.mastery.list_for_owner(1)) == 1  # upsert, not duplicate

    assert store.reviews.create(
        owner_user_id=1, objective_id=objective_id, concept="c1",
        next_review="2026-02-01",
    ) is not None
    # INSERT OR IGNORE -> duplicate create is a no-op, still returns the row id
    assert store.reviews.create(
        owner_user_id=1, objective_id=objective_id, concept="c1",
        next_review="2026-02-01",
    ) is not None
    assert len(store.reviews.list(1)) == 1

    assert store.plans.save(1, "2026-01-01", [{"x": 1}], "adaptive", 30, False) is True
    assert store.plans.save(1, "2026-01-01", [{"x": 2}], "adaptive", 45, True) is True
    plan = store.plans.get(1, "2026-01-01")
    assert plan["total_minutes"] == 45
    assert len(store.plans.recent(1)) == 1

    assert store.progress.record(1, "2026-01-01", study_minutes=10) is True
    assert store.progress.record(1, "2026-01-01", study_minutes=20) is True
    assert store.progress.get(1, "2026-01-01")["study_minutes"] == 30


def test_learning_misconceptions_events_and_clear_owner(pg_learning):
    store = pg_learning
    goal_id = store.goals.create(owner_user_id=1, kind="long_term", title="G")
    objective_id = store.objectives.create(owner_user_id=1, goal_id=goal_id, title="O")

    assert store.misconceptions.record(1, objective_id, "confuse", "e1") is True
    assert store.misconceptions.record(1, objective_id, "confuse", "e2") is True
    mis = store.misconceptions.list(1)
    assert len(mis) == 1 and mis[0]["count"] == 2

    assert store.events.log(1, "session_started", objective_id=objective_id) is True
    assert len(store.events.list(1)) == 1

    assert store.clear_owner(1) is True
    assert store.goals.list(1) == []
    assert store.misconceptions.list(1) == []
    assert store.events.list(1) == []


# ============================ MEMORY ============================


def test_memory_crud_lifecycle_and_ids(pg_memory):
    from app.memory.models import MemoryItem, build_hash_key

    store = pg_memory
    item = MemoryItem(
        owner_user_id=1, scope="USER", memory_type="profile",
        subject="Name", claim="The user is called Sam",
        hash_key=build_hash_key(1, "USER", "profile", "Name", "The user is called Sam"),
    )
    memory_id = store.memories.create(item)
    assert isinstance(memory_id, int) and memory_id > 0

    loaded = store.memories.get(1, memory_id)
    assert loaded is not None and loaded.claim == "The user is called Sam"
    assert store.memories.get(2, memory_id) is None  # owner isolation

    version_id = store.versions.add(
        memory_id=memory_id, owner_user_id=1, version=2,
        claim="Updated claim", confidence="high", importance=4, reason="edit",
    )
    assert isinstance(version_id, int) and version_id > 0
    assert len(store.versions.list(1, memory_id)) == 1

    event_id = store.events.log(
        owner_user_id=1, memory_id=memory_id, action="created", source="test"
    )
    assert isinstance(event_id, int) and event_id > 0
    assert len(store.events.list(1)) == 1

    store.access.log(owner_user_id=1, memory_id=memory_id)
    assert store.access.count(1) == 1


def test_memory_prefs_upsert_on_conflict(pg_memory):
    store = pg_memory
    prefs = store.prefs.ensure(1)
    assert prefs.auto_memory_enabled is True

    assert store.prefs.set_auto_enabled(1, False) is True
    assert store.prefs.get(1).auto_memory_enabled is False

    assert store.prefs.set_consent(1, "profile", "automatic") is True
    prefs = store.prefs.get(1)
    assert prefs.consent_types["profile"] == "automatic"


# ============================ TRANSACTIONS / CONCURRENCY ============================


def test_transaction_rollback_discards_writes(pg_db):
    pg_db.create_user(1, "u1", "U1")
    conn = pg_db._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO activity_log (user_id, action, details) VALUES (%s, %s, %s)",
        (1, "temp", "should be rolled back"),
    )
    conn.rollback()

    cursor.execute("SELECT COUNT(*) FROM activity_log WHERE action = %s", ("temp",))
    assert cursor.fetchone()[0] == 0


def test_concurrent_writes_across_threads(pg_db):
    pg_db.create_user(1, "u1", "U1")
    errors = []

    def worker(n):
        try:
            pg_db.create_user(1000 + n, f"user{n}", f"User {n}")
            goal_id = pg_db.create_goal(1, f"goal {n}", "", "cat", [], None)
            assert isinstance(goal_id, int) and goal_id > 0
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            pg_db._manager._close_connection()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(pg_db.get_goals(1)) == 8
    assert len(pg_db.get_all_user_ids()) == 9


# ============================ SCHEDULER ============================


class _FakeJob:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class _FakeJobQueue:
    def __init__(self):
        self.jobs_list = []

    def jobs(self):
        return list(self.jobs_list)

    def _mk(self, name, **kwargs):
        job = _FakeJob(name, **kwargs)
        self.jobs_list.append(job)
        return job

    def run_daily(self, callback, time=None, name=None, data=None):
        return self._mk(name, time=time, data=data)

    def run_once(self, callback, when=None, name=None, data=None):
        return self._mk(name, when=when, data=data)


class _FakeApp:
    def __init__(self):
        self.job_queue = _FakeJobQueue()
        self.bot_data = {}


async def test_scheduler_resync_on_postgres(pg_db):
    from app.infrastructure.scheduler import ReminderScheduler

    pg_db.create_user(10, "u", "U")
    one_off = pg_db.create_reminder(10, "custom", "t", "m", "2026-10-01T08:00:00", False)
    recurring = pg_db.create_reminder(10, "custom", "t2", "m2", "2026-10-02T09:00:00", True)

    scheduler = ReminderScheduler(_FakeApp(), database=pg_db)
    scheduler.running = True
    await scheduler.resync()

    names = {job.name for job in scheduler.application.job_queue.jobs_list}
    assert f"custom_reminder_{one_off}" in names
    assert f"custom_reminder_{recurring}" in names
