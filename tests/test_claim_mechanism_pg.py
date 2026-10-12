"""H1 - ingestion claim/lease mechanism on real PostgreSQL.

Companion to ``tests/test_claim_mechanism.py`` (SQLite). It runs the SAME
claim repository code paths against a real PostgreSQL server and adds the
concurrency proofs that only a real multi-connection server can give:

- genuinely overlapping transactions (one connection holds the claim row's
  lock inside an open transaction while another connection attempts takeover;
  the takeover must block and then resolve consistently),
- independent-connection acquisition races (exactly one winner),
- PostgreSQL clock semantics for lease expiry (``EXTRACT(EPOCH FROM now())``).

An environment guard skips the whole module only when PostgreSQL is genuinely
unreachable - never to hide a failure. No production database is touched: a
dedicated throwaway database is created and dropped per session.
"""

import os
import threading
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

_PG_HOST = os.environ.get("DB_HOST", "127.0.0.1")
_PG_PORT = int(os.environ.get("DB_PORT", "5432"))
_PG_USER = os.environ.get("DB_USER", "austro")
_PG_PASSWORD = os.environ.get("DB_PASSWORD", "austro_test_pw")
_PG_TEST_DB = os.environ.get("DB_TEST_NAME", "austro_ai_pgtest")
_PG_ADMIN_USER = os.environ.get("DB_ADMIN_USER", "postgres")
_PG_ADMIN_PASSWORD = os.environ.get("DB_ADMIN_PASSWORD", _PG_PASSWORD)

OWNER = 7


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
    """A fresh ``Database`` facade on a clean PostgreSQL schema."""
    import app.database as database_module
    import app.database.connection as connection_module
    import app.database.migrations as migrations_module
    from app.config.settings import settings

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
def pg_store(pg_db):
    from app.knowledge.repositories import KnowledgeStore

    return KnowledgeStore(pg_db._manager)


def _make_source(store, owner=OWNER) -> int:
    checksum = uuid.uuid4().hex
    source_id = store.sources.create(
        owner_user_id=owner,
        source_type="book",
        title=f"claim-pg-{checksum[:8]}",
        file_name=f"{checksum}.txt",
        file_format="txt",
        mime_type="text/plain",
        file_size_bytes=16,
        checksum=checksum,
        storage_key=f"tests/{checksum}",
        original_ref=None,
    )
    assert isinstance(source_id, int) and source_id > 0
    return source_id


def _acquire(store, source_id, token, ttl=30, worker="w1", owner=OWNER):
    return store.claims.acquire(
        source_id=source_id,
        owner_user_id=owner,
        worker_id=worker,
        claim_token=token,
        ttl_seconds=ttl,
    )


def _claim_row(store, source_id):
    conn = store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM knowledge_source_claims WHERE source_id = %s", (source_id,)
    )
    row = cursor.fetchone()
    conn.commit()
    return dict(row) if row is not None else None


def _expire(store, source_id):
    conn = store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE knowledge_source_claims SET lease_expires_at = 0 WHERE source_id = %s",
        (source_id,),
    )
    conn.commit()


# ============================ LIFECYCLE ============================


def test_claim_lifecycle_on_postgres(pg_store):
    source_id = _make_source(pg_store)

    first = _acquire(pg_store, source_id, "tok-1", ttl=30, worker="w1")
    assert first is not None
    row = _claim_row(pg_store, source_id)
    assert row["claim_token"] == "tok-1"
    assert row["attempt"] == 1
    assert row["lease_expires_at"] > row["claimed_at"]

    # Competing claim rejected while the lease is valid.
    assert _acquire(pg_store, source_id, "tok-2", ttl=30) is None

    # Expiry -> takeover increments the fencing attempt.
    _expire(pg_store, source_id)
    taken = _acquire(pg_store, source_id, "tok-2", ttl=30, worker="w2")
    assert taken is not None
    row = _claim_row(pg_store, source_id)
    assert row["claim_token"] == "tok-2"
    assert row["attempt"] == 2

    # Renewal: owner ok, wrong token refused, old token refused after takeover.
    assert pg_store.claims.renew(source_id=source_id, claim_token="tok-2", ttl_seconds=60) is True
    assert pg_store.claims.renew(source_id=source_id, claim_token="wrong", ttl_seconds=60) is False
    assert pg_store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=60) is False

    # Release is token-validated.
    assert pg_store.claims.release(source_id=source_id, claim_token="tok-1") is False
    assert pg_store.claims.release(source_id=source_id, claim_token="tok-2") is True
    assert _claim_row(pg_store, source_id)["status"] == "released"


def test_renewal_after_expiration_refused_on_postgres(pg_store):
    source_id = _make_source(pg_store)
    assert _acquire(pg_store, source_id, "tok-1", ttl=30) is not None
    _expire(pg_store, source_id)
    assert (
        pg_store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=30)
        is False
    )


def test_fenced_writes_and_publication_on_postgres(pg_store):
    source_id = _make_source(pg_store)
    assert _acquire(pg_store, source_id, "tok-1", ttl=30) is not None
    _expire(pg_store, source_id)
    assert _acquire(pg_store, source_id, "tok-2", ttl=30) is not None

    fresh_doc = pg_store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="fresh",
        author=None,
        language="en",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-2",
    )
    assert isinstance(fresh_doc, int)

    from app.core.errors import ClaimLostError

    with pytest.raises(ClaimLostError):
        pg_store.documents.create(
            source_id=source_id,
            owner_user_id=OWNER,
            title="stale",
            author=None,
            language="en",
            toc=[],
            total_chars=1,
            total_pages=1,
            claim_token="tok-1",
        )
    with pytest.raises(ClaimLostError):
        pg_store.claims.publish(
            source_id=source_id,
            owner_user_id=OWNER,
            claim_token="tok-1",
            document_id=fresh_doc,
            total_sections=0,
            title="stale",
            author=None,
            language="en",
            pages=1,
            char_count=1,
            metadata={},
        )

    assert pg_store.claims.publish(
        source_id=source_id,
        owner_user_id=OWNER,
        claim_token="tok-2",
        document_id=fresh_doc,
        total_sections=0,
        title="published",
        author=None,
        language="en",
        pages=1,
        char_count=10,
        metadata={},
    ) is True
    conn = pg_store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status FROM knowledge_sources WHERE source_id = %s", (source_id,)
    )
    assert cursor.fetchone()[0] == "completed"
    conn.commit()


def test_cleanup_preserves_newer_worker_data_on_postgres(pg_store):
    source_id = _make_source(pg_store)
    assert _acquire(pg_store, source_id, "tok-1", ttl=30) is not None
    stale_doc = pg_store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="stale",
        author=None,
        language="en",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-1",
    )
    _expire(pg_store, source_id)
    assert _acquire(pg_store, source_id, "tok-2", ttl=30) is not None
    fresh_doc = pg_store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="fresh",
        author=None,
        language="en",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-2",
    )

    result = pg_store.claims.cleanup_attempt(
        source_id=source_id,
        owner_user_id=OWNER,
        claim_token="tok-1",
        error="stale",
        mark_failed=True,
    )
    assert result["marked_failed"] is False
    assert result["deleted"]["knowledge_documents"] == 1

    conn = pg_store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT document_id FROM knowledge_documents WHERE source_id = %s",
        (source_id,),
    )
    remaining = [row[0] for row in cursor.fetchall()]
    conn.commit()
    assert remaining == [fresh_doc]
    assert stale_doc not in remaining
    assert _claim_row(pg_store, source_id)["claim_token"] == "tok-2"


# ============================ CONCURRENCY ============================


def test_concurrent_acquisition_single_winner_on_postgres(pg_db):
    """Independent connections race the same source: exactly one winner."""
    from app.knowledge.repositories import KnowledgeStore

    store = KnowledgeStore(pg_db._manager)
    source_id = _make_source(store)
    thread_count = 6
    barrier = threading.Barrier(thread_count)
    results = []

    managers = []

    def worker(index: int) -> None:
        from app.database.connection import DatabaseManager

        manager = DatabaseManager()
        managers.append(manager)
        claims = KnowledgeStore(manager).claims
        barrier.wait()
        got = claims.acquire(
            source_id=source_id,
            owner_user_id=OWNER,
            worker_id=f"worker-{index}",
            claim_token=f"tok-{index}",
            ttl_seconds=30,
        )
        results.append((index, got is not None))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        for manager in managers:
            manager._close_connection()

    winners = [index for index, won in results if won]
    assert len(results) == thread_count
    assert len(winners) == 1, f"exactly one acquirer must win: {results}"
    row = _claim_row(store, source_id)
    assert row["claim_token"] == f"tok-{winners[0]}"
    assert row["attempt"] == 1


def test_takeover_blocks_on_open_transaction(pg_store):
    """A genuinely overlapping transaction: one connection holds the claim
    row's lock (SELECT ... FOR UPDATE, uncommitted) while another connection
    attempts takeover. The takeover must block until the lock is released and
    then resolve exactly once."""
    source_id = _make_source(pg_store)
    assert _acquire(pg_store, source_id, "tok-1", ttl=30) is not None
    _expire(pg_store, source_id)

    holder = _connect(_PG_TEST_DB)
    holder.autocommit = False
    holder_cur = holder.cursor()
    holder_cur.execute(
        "SELECT * FROM knowledge_source_claims WHERE source_id = %s "
        "FOR UPDATE",
        (source_id,),
    )
    assert holder_cur.fetchone() is not None

    done = threading.Event()
    outcome = {}

    def take_over():
        from app.database.connection import DatabaseManager
        from app.knowledge.repositories import KnowledgeStore

        manager = DatabaseManager()
        try:
            got = KnowledgeStore(manager).claims.acquire(
                source_id=source_id,
                owner_user_id=OWNER,
                worker_id="worker-2",
                claim_token="tok-2",
                ttl_seconds=30,
            )
            outcome["got"] = got is not None
        finally:
            manager._close_connection()
            done.set()

    thread = threading.Thread(target=take_over)
    thread.start()
    # The takeover must be blocked while the row lock is held.
    assert done.wait(0.5) is False, "takeover must wait for the open transaction"

    holder.rollback()  # release the row lock
    holder.close()
    assert done.wait(30) is True, "takeover must complete after the lock release"
    thread.join(timeout=30)

    assert outcome["got"] is True
    row = _claim_row(pg_store, source_id)
    assert row["claim_token"] == "tok-2"
    assert row["attempt"] == 2


def test_lease_expiry_uses_postgresql_clock(pg_store):
    """The PostgreSQL time fragment is used: a lease set to the DB's own
    current epoch second is expired (strict ``> now`` comparison)."""
    source_id = _make_source(pg_store)
    assert _acquire(pg_store, source_id, "tok-1", ttl=30) is not None

    conn = pg_store._manager._get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE knowledge_source_claims SET lease_expires_at = "
        "CAST(EXTRACT(EPOCH FROM now()) AS INTEGER) WHERE source_id = %s",
        (source_id,),
    )
    conn.commit()
    assert pg_store.claims.owns(source_id, "tok-1") is False
    assert (
        pg_store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=30)
        is False
    )
