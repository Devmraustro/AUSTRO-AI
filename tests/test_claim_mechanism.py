"""H1 - ingestion claim/lease mechanism tests (SQLite, real persisted state).

Covers the database-backed claim and lease added for concurrency hardening:

- atomic claim acquisition (including a multi-connection race),
- rejection of competing workers holding a valid claim,
- safe takeover of expired claims and of released claims,
- token-validated release and renewal (wrong token / after expiration /
  after takeover are all refused),
- renewal failure signalling and stopping active processing,
- stale-worker intermediate-write prevention (fenced writes),
- stale-worker finalization prevention (fenced publication),
- cleanup that can never delete a newer worker's data,
- retry and failure handling across attempts,
- long-running ingestion with short configurable TTLs,
- cancellation cleanup,
- migration compatibility and repeatability.

Every assertion reads the persisted rows; nothing here is mocked except
deliberate failure/latency injection into the embedder (one stage) and the
simulated passage of lease time (an UPDATE that moves ``lease_expires_at``
into the past - the lease comparisons themselves always run in SQL against
the database clock).
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
import uuid

import pytest

from app.config.settings import settings as app_settings
from app.core.container import build_container
from app.core.errors import ClaimLostError
from app.database.connection import DatabaseManager
from app.database.migrations import (
    KNOWLEDGE_V1,
    KNOWLEDGE_V2,
    _MIGRATIONS_TABLE,
    apply_migrations,
)
from app.knowledge.models import (
    COMPLETED_STATUS,
    EmbeddingRecord,
    FAILED_STATUS,
    PROCESSING_STATUS,
)
from app.knowledge.repositories import KnowledgeStore

OWNER = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db():
    import database

    return database.db


def _sql(sql: str, params=()):
    conn = _db()._get_connection()
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    conn.commit()
    return rows


def _count(table: str, where: str = "", params=()) -> int:
    rows = _sql(f"SELECT COUNT(*) AS n FROM {table} {where}", params)  # nosec B608
    return int(rows[0]["n"])


def _claim_row(source_id: int):
    rows = _sql(
        "SELECT * FROM knowledge_source_claims WHERE source_id = ?", (source_id,)
    )
    return dict(rows[0]) if rows else None


def _expire(source_id: int) -> None:
    """Simulate the lease running out (the DB clock is the only clock)."""
    _sql(
        "UPDATE knowledge_source_claims SET lease_expires_at = 0 WHERE source_id = ?",
        (source_id,),
    )


def _make_source(store, owner: int = OWNER) -> int:
    checksum = uuid.uuid4().hex
    source_id = store.sources.create(
        owner_user_id=owner,
        source_type="book",
        title=f"claim-test-{checksum[:8]}",
        file_name=f"{checksum}.txt",
        file_format="txt",
        mime_type="text/plain",
        file_size_bytes=16,
        checksum=checksum,
        storage_key=f"tests/{checksum}",
        original_ref=None,
    )
    assert source_id is not None
    return source_id


def _acquire(store, source_id, token, ttl=30, worker="w1", owner=OWNER):
    return store.claims.acquire(
        source_id=source_id,
        owner_user_id=owner,
        worker_id=worker,
        claim_token=token,
        ttl_seconds=ttl,
    )


@pytest.fixture()
def knowledge(fresh_db):
    return build_container().knowledge


@pytest.fixture()
def store(knowledge):
    return knowledge._store


UNIQUE_BOOK = (
    "الفصل الأول\n"
    "مقدمة في إدارة الوقت\n\n"
    "إدارة الوقت مهارة أساسية تساعد الإنسان على إنجاز مهامه اليومية بتركيز "
    "وعمق دون تشتت، ويتعلم منها كيف ينظم أولوياته.\n\n"
    "الفصل الثاني\n"
    "بناء العادات الصغيرة\n\n"
    "العادات الصغيرة المتراكمة تصنع فرقاً كبيراً مع مرور الوقت، والالتزام "
    "اليومي المتسق هو سر النجاح طويل المدى في بناء أي عادة جديدة.\n"
)


async def _upload(knowledge, owner: int = OWNER, text: str = UNIQUE_BOOK):
    payload = f"{text}\n{uuid.uuid4()}".encode("utf-8")
    return await knowledge.register_upload(
        owner_user_id=owner,
        file_name=f"{uuid.uuid4().hex}.txt",
        data=payload,
        mime_type="text/plain",
    )


def _derived_counts(source_id: int) -> dict:
    return {
        "knowledge_documents": _count(
            "knowledge_documents", "WHERE source_id = ?", (source_id,)
        ),
        "knowledge_sections": _count(
            "knowledge_sections", "WHERE source_id = ?", (source_id,)
        ),
        "knowledge_chunks": _count(
            "knowledge_chunks", "WHERE source_id = ?", (source_id,)
        ),
        "knowledge_embeddings": _count(
            "knowledge_embeddings", "WHERE source_id = ?", (source_id,)
        ),
    }


# ---------------------------------------------------------------------------
# Claim acquisition / competition / takeover
# ---------------------------------------------------------------------------


def test_successful_and_competing_claims(store):
    source_id = _make_source(store)

    first = _acquire(store, source_id, "tok-1", ttl=30, worker="worker-1")
    assert first is not None
    assert first["claim_token"] == "tok-1"
    assert first["attempt"] == 1

    row = _claim_row(source_id)
    assert row["status"] == "active"
    assert row["worker_id"] == "worker-1"
    assert row["claim_token"] == "tok-1"
    # The lease is strictly longer than the acquisition instant.
    assert row["lease_expires_at"] > row["claimed_at"]

    # A competing worker is rejected while the lease is valid.
    second = _acquire(store, source_id, "tok-2", ttl=30, worker="worker-2")
    assert second is None
    assert _claim_row(source_id)["claim_token"] == "tok-1"


def test_expired_claim_is_taken_over_safely(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None

    _expire(source_id)
    taken = _acquire(store, source_id, "tok-2", ttl=30, worker="worker-2")
    assert taken is not None
    assert taken["claim_token"] == "tok-2"

    row = _claim_row(source_id)
    assert row["claim_token"] == "tok-2"
    assert row["worker_id"] == "worker-2"
    # Monotonic fencing number increases on every takeover.
    assert row["attempt"] == 2
    assert row["status"] == "active"

    # And the fresh lease rejects competitors again.
    assert _acquire(store, source_id, "tok-3", ttl=30) is None


def test_released_claim_can_be_reacquired(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    assert store.claims.release(source_id=source_id, claim_token="tok-1") is True

    again = _acquire(store, source_id, "tok-2", ttl=30)
    assert again is not None
    assert again["attempt"] == 2


def test_atomic_claim_acquisition_across_connections(store, fresh_db):
    """N independent connections race for one source: exactly one wins."""
    source_id = _make_source(store)
    path = _db().db_path
    thread_count = 6

    managers = [DatabaseManager(path) for _ in range(thread_count)]
    barrier = threading.Barrier(thread_count)
    results = []

    def worker(index: int) -> None:
        claims = KnowledgeStore(managers[index]).claims
        barrier.wait()
        got = claims.acquire(
            source_id=source_id,
            owner_user_id=OWNER,
            worker_id=f"worker-{index}",
            claim_token=f"tok-{index}",
            ttl_seconds=30,
        )
        results.append((index, got is not None))

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(thread_count)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        for manager in managers:
            manager._close_connection()

    winners = [index for index, won in results if won]
    assert len(results) == thread_count
    assert len(winners) == 1, f"exactly one acquirer must win: {results}"
    row = _claim_row(source_id)
    assert row["claim_token"] == f"tok-{winners[0]}"
    assert row["attempt"] == 1


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_valid_and_invalid_release(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None

    assert store.claims.release(source_id=source_id, claim_token="wrong") is False
    row = _claim_row(source_id)
    assert row["status"] == "active"
    assert row["released_at"] is None

    assert store.claims.release(source_id=source_id, claim_token="tok-1") is True
    row = _claim_row(source_id)
    assert row["status"] == "released"
    assert row["released_at"] is not None

    # Double release and wrong-token release are refused.
    assert store.claims.release(source_id=source_id, claim_token="tok-1") is False
    assert store.claims.release(source_id=source_id, claim_token="wrong") is False

    # After ownership transfer the old token can never release.
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None
    assert store.claims.release(source_id=source_id, claim_token="tok-1") is False
    assert _claim_row(source_id)["status"] == "active"


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


def test_renewal_by_current_owner_extends_lease(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=1) is not None
    before = _claim_row(source_id)

    assert (
        store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=120)
        is True
    )
    after = _claim_row(source_id)
    assert after["renewed_at"] is not None
    assert after["lease_expires_at"] >= before["lease_expires_at"] + 100


def test_renewal_with_wrong_token_is_refused(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=60) is not None

    assert (
        store.claims.renew(source_id=source_id, claim_token="wrong", ttl_seconds=60)
        is False
    )
    row = _claim_row(source_id)
    assert row["claim_token"] == "tok-1"
    assert row["renewed_at"] is None


def test_renewal_after_expiration_is_refused(store):
    """Even the correct token cannot renew once the lease has expired."""
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    _expire(source_id)

    assert (
        store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=30)
        is False
    )
    assert _claim_row(source_id)["renewed_at"] is None


def test_renewal_after_takeover_is_refused(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    _expire(source_id)
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None

    assert (
        store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=30)
        is False
    )
    assert (
        store.claims.renew(source_id=source_id, claim_token="tok-2", ttl_seconds=30)
        is True
    )


def test_lease_boundary_is_evaluated_by_the_database_clock(store):
    """At the exact expiry instant the lease is dead: owns() is False and the
    row comparison is strict (``lease_expires_at > now``)."""
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    # Set expiry to the DB's current second: no longer strictly greater.
    _sql(
        "UPDATE knowledge_source_claims SET lease_expires_at = "
        "CAST(strftime('%s','now') AS INTEGER) WHERE source_id = ?",
        (source_id,),
    )
    assert store.claims.owns(source_id, "tok-1") is False
    assert (
        store.claims.renew(source_id=source_id, claim_token="tok-1", ttl_seconds=30)
        is False
    )


# ---------------------------------------------------------------------------
# Stale-worker write / finalization prevention
# ---------------------------------------------------------------------------


def test_expired_worker_cannot_write_before_takeover(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    _expire(source_id)

    # Same token, but the lease is gone: the database refuses the write.
    with pytest.raises(ClaimLostError):
        store.documents.create(
            source_id=source_id,
            owner_user_id=OWNER,
            title="stale",
            author=None,
            language="ar",
            toc=[],
            total_chars=1,
            total_pages=1,
            claim_token="tok-1",
        )
    assert _count("knowledge_documents") == 0


def test_stale_worker_intermediate_writes_are_refused(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    _expire(source_id)
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None

    # The new owner can write (control).
    document_id = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="fresh",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-2",
    )
    assert document_id is not None

    # Every intermediate write by the stale epoch is refused by the DB fence.
    with pytest.raises(ClaimLostError):
        store.documents.create(
            source_id=source_id,
            owner_user_id=OWNER,
            title="stale-document",
            author=None,
            language="ar",
            toc=[],
            total_chars=1,
            total_pages=1,
            claim_token="tok-1",
        )
    with pytest.raises(ClaimLostError):
        store.sections.create(
            document_id=document_id,
            owner_user_id=OWNER,
            source_id=source_id,
            level=0,
            title="stale-section",
            order_index=0,
            start_char=0,
            end_char=1,
            claim_token="tok-1",
        )
    with pytest.raises(ClaimLostError):
        store.chunks.create(
            owner_user_id=OWNER,
            source_id=source_id,
            document_id=document_id,
            section_id=None,
            chunk_key="stale-key",
            content="stale",
            content_hash="stale-hash",
            token_count=1,
            char_count=5,
            page=None,
            order_index=0,
            claim_token="tok-1",
        )
    with pytest.raises(ClaimLostError):
        store.embeddings.save_many(
            [
                EmbeddingRecord(
                    owner_user_id=OWNER,
                    source_id=source_id,
                    chunk_row_id=1,
                    model="m",
                    version="1",
                    dimensions=2,
                    vector=[0.0, 0.0],
                )
            ],
            claim_token="tok-1",
        )
    with pytest.raises(ClaimLostError):
        store.sources.set_state(
            OWNER, source_id, "completed", "READY", claim_token="tok-1"
        )

    # Persisted state: nothing from the stale epoch exists; the live row is
    # untouched and the source state was not stomped.
    counts = _derived_counts(source_id)
    assert counts["knowledge_documents"] == 1
    assert counts["knowledge_sections"] == 0
    assert counts["knowledge_chunks"] == 0
    assert counts["knowledge_embeddings"] == 0
    source_row = _sql(
        "SELECT status, ingestion_state FROM knowledge_sources WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] != "completed"


def test_stale_worker_finalization_is_refused(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    document_id = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="stale-doc",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-1",
    )
    assert document_id is not None

    _expire(source_id)
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None

    with pytest.raises(ClaimLostError):
        store.documents.complete(
            OWNER, document_id, total_sections=1, claim_token="tok-1"
        )
    with pytest.raises(ClaimLostError):
        store.claims.publish(
            source_id=source_id,
            owner_user_id=OWNER,
            claim_token="tok-1",
            document_id=document_id,
            total_sections=1,
            title="stale",
            author=None,
            language="ar",
            pages=1,
            char_count=10,
            metadata={},
        )

    source_row = _sql(
        "SELECT status FROM knowledge_sources WHERE source_id = ?", (source_id,)
    )[0]
    assert source_row["status"] != COMPLETED_STATUS
    document_row = _sql(
        "SELECT status FROM knowledge_documents WHERE document_id = ?",
        (document_id,),
    )[0]
    assert document_row["status"] == "processing"


def test_publication_is_atomic_and_releases_the_claim(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    document_id = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="doc",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-1",
    )
    assert store.claims.publish(
        source_id=source_id,
        owner_user_id=OWNER,
        claim_token="tok-1",
        document_id=document_id,
        total_sections=3,
        title="published-title",
        author="author",
        language="ar",
        pages=2,
        char_count=10,
        metadata={"k": "v"},
    ) is True

    source_row = dict(
        _sql(
            "SELECT * FROM knowledge_sources WHERE source_id = ?", (source_id,)
        )[0]
    )
    assert source_row["status"] == COMPLETED_STATUS
    assert source_row["title"] == "published-title"
    assert source_row["pages"] == 2
    document_row = dict(
        _sql(
            "SELECT * FROM knowledge_documents WHERE document_id = ?",
            (document_id,),
        )[0]
    )
    assert document_row["status"] == COMPLETED_STATUS
    assert document_row["total_sections"] == 3
    assert _claim_row(source_id)["status"] == "released"

    # A second publication with the same (now released) claim is refused.
    with pytest.raises(ClaimLostError):
        store.claims.publish(
            source_id=source_id,
            owner_user_id=OWNER,
            claim_token="tok-1",
            document_id=document_id,
            total_sections=3,
            title="again",
            author=None,
            language="ar",
            pages=2,
            char_count=10,
            metadata={},
        )


# ---------------------------------------------------------------------------
# Cleanup semantics
# ---------------------------------------------------------------------------


def test_cleanup_preserves_newer_worker_data(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None

    def write_rows(token: str, version: int) -> dict:
        document_id = store.documents.create(
            source_id=source_id,
            owner_user_id=OWNER,
            title=f"doc-{token}",
            author=None,
            language="ar",
            toc=[],
            total_chars=10,
            total_pages=1,
            version=version,
            claim_token=token,
        )
        section_id = store.sections.create(
            document_id=document_id,
            owner_user_id=OWNER,
            source_id=source_id,
            level=0,
            title=f"section-{token}",
            order_index=0,
            start_char=0,
            end_char=1,
            claim_token=token,
        )
        chunk_id = store.chunks.create(
            owner_user_id=OWNER,
            source_id=source_id,
            document_id=document_id,
            section_id=section_id,
            chunk_key=f"key-{token}-{uuid.uuid4()}",
            content=f"content-{token}",
            content_hash=f"hash-{token}",
            token_count=1,
            char_count=10,
            page=None,
            order_index=0,
            claim_token=token,
        )
        store.embeddings.save_many(
            [
                EmbeddingRecord(
                    owner_user_id=OWNER,
                    source_id=source_id,
                    chunk_row_id=chunk_id,
                    model="m",
                    version="1",
                    dimensions=2,
                    vector=[0.1, 0.2],
                )
            ],
            claim_token=token,
        )
        return {
            "document_id": document_id,
            "section_id": section_id,
            "chunk_id": chunk_id,
        }

    stale_rows = write_rows("tok-1", version=1)
    _expire(source_id)
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None
    fresh_rows = write_rows("tok-2", version=2)

    result = store.claims.cleanup_attempt(
        source_id=source_id,
        owner_user_id=OWNER,
        claim_token="tok-1",
        error="stale failure",
        mark_failed=True,
    )
    assert result["marked_failed"] is False  # no longer the owner
    assert result["released"] is False

    # The stale attempt's rows are gone; the newer worker's rows survive.
    assert _count(
        "knowledge_documents", "WHERE document_id = ?", (stale_rows["document_id"],)
    ) == 0
    assert _count(
        "knowledge_sections", "WHERE section_id = ?", (stale_rows["section_id"],)
    ) == 0
    assert _count(
        "knowledge_chunks", "WHERE chunk_id = ?", (stale_rows["chunk_id"],)
    ) == 0
    assert _count(
        "knowledge_documents", "WHERE document_id = ?", (fresh_rows["document_id"],)
    ) == 1
    assert _count(
        "knowledge_sections", "WHERE section_id = ?", (fresh_rows["section_id"],)
    ) == 1
    assert _count(
        "knowledge_chunks", "WHERE chunk_id = ?", (fresh_rows["chunk_id"],)
    ) == 1
    assert _count(
        "knowledge_embeddings", "WHERE chunk_row_id = ?", (fresh_rows["chunk_id"],)
    ) == 1

    # The source state was not touched by the stale attempt, and the newer
    # claim is still intact.
    source_row = _sql(
        "SELECT status, error_message FROM knowledge_sources WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] != FAILED_STATUS
    assert source_row["error_message"] != "stale failure"
    claim = _claim_row(source_id)
    assert claim["claim_token"] == "tok-2"
    assert claim["status"] == "active"


def test_cleanup_by_owner_marks_failed_and_releases(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    document_id = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="doc",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-1",
    )
    assert document_id is not None

    result = store.claims.cleanup_attempt(
        source_id=source_id,
        owner_user_id=OWNER,
        claim_token="tok-1",
        error="boom",
        mark_failed=True,
    )
    assert result["marked_failed"] is True
    assert result["released"] is True
    assert result["deleted"]["knowledge_documents"] == 1

    source_row = _sql(
        "SELECT status, error_message, retry_count FROM knowledge_sources "
        "WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] == FAILED_STATUS
    assert source_row["error_message"] == "boom"
    assert source_row["retry_count"] == 1
    assert _derived_counts(source_id) == {
        "knowledge_documents": 0,
        "knowledge_sections": 0,
        "knowledge_chunks": 0,
        "knowledge_embeddings": 0,
    }
    assert _claim_row(source_id)["status"] == "released"


def test_purge_unowned_rows_only_spares_current_epoch(store):
    source_id = _make_source(store)
    assert _acquire(store, source_id, "tok-1", ttl=30) is not None
    store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="dead-epoch",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        claim_token="tok-1",
    )
    # Legacy rows from before claim tagging must not be assumed stale.
    store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="legacy-untagged",
        author=None,
        language="ar",
        toc=[],
        total_chars=5,
        total_pages=1,
        claim_token=None,
    )
    _expire(source_id)
    assert _acquire(store, source_id, "tok-2", ttl=30) is not None
    kept = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="current-epoch",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        version=2,
        claim_token="tok-2",
    )
    assert kept is not None

    deleted = store.claims.purge_unowned_rows(
        source_id=source_id, claim_token="tok-2"
    )
    assert deleted["knowledge_documents"] == 1
    rows = _sql(
        "SELECT title FROM knowledge_documents WHERE source_id = ?",
        (source_id,),
    )
    # Assert persisted row identity by title: the repository's create method
    # does not promise a document ID for legacy/null-token rows.
    assert {row["title"] for row in rows} == {"legacy-untagged", "current-epoch"}


# ---------------------------------------------------------------------------
# Pipeline integration (async): renewal, cancellation, retry
# ---------------------------------------------------------------------------


async def test_competing_process_source_is_rejected_while_lease_is_held(
    knowledge, monkeypatch
):
    upload = await _upload(knowledge)
    source_id = upload["source_id"]

    started = threading.Event()
    release = threading.Event()
    real_embed = knowledge.pipeline._embeddings.embed_many

    def blocking_embed(texts):
        started.set()
        assert release.wait(20)
        return real_embed(texts)

    monkeypatch.setattr(
        knowledge.pipeline._embeddings, "embed_many", blocking_embed
    )

    first_run = asyncio.create_task(knowledge.process_source(OWNER, source_id))
    assert await asyncio.to_thread(started.wait, 20) is True

    # Second worker arrives while the first holds a valid claim.
    rejected = await knowledge.process_source(OWNER, source_id)
    assert rejected.status == PROCESSING_STATUS
    assert rejected.error == "المعالجة جارية بالفعل"

    release.set()
    result = await first_run
    assert result.status == COMPLETED_STATUS

    source_row = _sql(
        "SELECT status FROM knowledge_sources WHERE source_id = ?", (source_id,)
    )[0]
    assert source_row["status"] == COMPLETED_STATUS
    # Exactly one published index exists.
    counts = _derived_counts(source_id)
    assert counts["knowledge_documents"] == 1
    assert counts["knowledge_chunks"] > 0
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]


async def test_renewal_failure_stops_active_processing(knowledge, monkeypatch):
    """The lease is taken over mid-run: renewal fails, the run stops, and the
    stale attempt's cleanup leaves the new owner's data untouched."""
    upload = await _upload(knowledge)
    source_id = upload["source_id"]
    store = knowledge._store

    started = threading.Event()
    release = threading.Event()
    real_embed = knowledge.pipeline._embeddings.embed_many

    def takeover_embed(texts):
        started.set()
        assert release.wait(20)
        return real_embed(texts)

    monkeypatch.setattr(
        knowledge.pipeline._embeddings, "embed_many", takeover_embed
    )

    run = asyncio.create_task(knowledge.process_source(OWNER, source_id))
    assert await asyncio.to_thread(started.wait, 20) is True

    # While the pipeline sits in EMBED, its lease expires and another worker
    # takes over and writes its own rows.
    stale_token = _claim_row(source_id)["claim_token"]
    _expire(source_id)
    assert _acquire(store, source_id, "tok-new", ttl=60, worker="worker-2") is not None
    new_document_id = store.documents.create(
        source_id=source_id,
        owner_user_id=OWNER,
        title="new-owner-document",
        author=None,
        language="ar",
        toc=[],
        total_chars=10,
        total_pages=1,
        version=2,
        claim_token="tok-new",
    )
    assert new_document_id is not None

    release.set()
    result = await run

    # The stale run stopped safely and did not publish.
    assert result.status == FAILED_STATUS
    assert result.error == "انتهت صلاحية معالجة الملف أو تولاها عامل آخر"
    source_row = _sql(
        "SELECT status FROM knowledge_sources WHERE source_id = ?", (source_id,)
    )[0]
    assert source_row["status"] != COMPLETED_STATUS

    # The new owner's row survives; no stale-tagged rows remain.
    assert _count(
        "knowledge_documents", "WHERE document_id = ?", (new_document_id,)
    ) == 1
    assert (
        _count(
            "knowledge_documents",
            "WHERE source_id = ? AND claim_token = ?",
            (source_id, stale_token),
        )
        == 0
    )
    claim = _claim_row(source_id)
    assert claim["claim_token"] == "tok-new"
    assert claim["status"] == "active"


async def test_long_running_ingestion_with_short_ttls(knowledge, monkeypatch):
    """A run longer than the TTL completes because the renewal loop keeps the
    lease alive (sub-minute TTL/renewal configured)."""
    config = dataclasses.replace(
        app_settings,
        knowledge_claim_ttl_seconds=2,
        knowledge_claim_renew_interval_seconds=0.3,
    )
    fast = build_container(settings_instance=config).knowledge
    upload = await _upload(fast)
    source_id = upload["source_id"]

    real_embed = fast.pipeline._embeddings.embed_many

    def slow_embed(texts):
        time.sleep(2.6)  # longer than the 2s lease TTL
        return real_embed(texts)

    monkeypatch.setattr(fast.pipeline._embeddings, "embed_many", slow_embed)

    result = await fast.process_source(OWNER, source_id)
    assert result.status == COMPLETED_STATUS

    claim = _claim_row(source_id)
    assert claim["renewed_at"] is not None, "the lease must have been renewed"
    assert claim["lease_expires_at"] > claim["claimed_at"]
    assert claim["status"] == "released"
    source_row = _sql(
        "SELECT status FROM knowledge_sources WHERE source_id = ?", (source_id,)
    )[0]
    assert source_row["status"] == COMPLETED_STATUS
    counts = _derived_counts(source_id)
    assert counts["knowledge_chunks"] > 0
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]


async def test_cancellation_cleans_attempt_and_marks_failed(
    knowledge, monkeypatch
):
    upload = await _upload(knowledge)
    source_id = upload["source_id"]

    started = threading.Event()

    def stuck_embed(texts):
        started.set()
        time.sleep(3)
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(
        knowledge.pipeline._embeddings, "embed_many", stuck_embed
    )

    run = asyncio.create_task(knowledge.process_source(OWNER, source_id))
    assert await asyncio.to_thread(started.wait, 20) is True
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    source_row = _sql(
        "SELECT status, error_message, retry_count FROM knowledge_sources "
        "WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] == FAILED_STATUS
    assert source_row["error_message"] == "cancelled"
    assert source_row["retry_count"] == 1
    assert _derived_counts(source_id) == {
        "knowledge_documents": 0,
        "knowledge_sections": 0,
        "knowledge_chunks": 0,
        "knowledge_embeddings": 0,
    }
    assert _claim_row(source_id)["status"] == "released"


async def test_retry_after_failure_reingests_cleanly(knowledge, monkeypatch):
    upload = await _upload(knowledge)
    source_id = upload["source_id"]

    real_embed = knowledge.pipeline._embeddings.embed_many
    calls = {"n": 0}

    def flaky_embed(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient embedder failure")
        return real_embed(texts)

    monkeypatch.setattr(
        knowledge.pipeline._embeddings, "embed_many", flaky_embed
    )

    first = await knowledge.process_source(OWNER, source_id)
    assert first.status == FAILED_STATUS
    source_row = _sql(
        "SELECT status, retry_count FROM knowledge_sources WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] == FAILED_STATUS
    assert source_row["retry_count"] == 1
    assert _derived_counts(source_id)["knowledge_documents"] == 0

    second = await knowledge.process_source(OWNER, source_id)
    assert second.status == COMPLETED_STATUS

    source_row = _sql(
        "SELECT status, retry_count, error_message FROM knowledge_sources "
        "WHERE source_id = ?",
        (source_id,),
    )[0]
    assert source_row["status"] == COMPLETED_STATUS
    assert source_row["error_message"] is None
    counts = _derived_counts(source_id)
    assert counts["knowledge_documents"] == 1
    assert counts["knowledge_chunks"] == second.chunk_count
    assert counts["knowledge_embeddings"] == second.chunk_count
    # The retry ran as a new claim epoch (takeover of the released claim).
    claim = _claim_row(source_id)
    assert claim["attempt"] == 2
    assert claim["status"] == "released"


# ---------------------------------------------------------------------------
# Migration compatibility / repeatability
# ---------------------------------------------------------------------------


def test_migration_adds_claim_schema(fresh_db):
    conn = _db()._get_connection()
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "knowledge_source_claims" in tables

    for table in ("knowledge_documents", "knowledge_sections",
                  "knowledge_chunks", "knowledge_embeddings"):
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
        }
        assert "claim_token" in columns, f"{table} must carry claim_token"

    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'knowledge'"
    ).fetchone()
    assert int(row["version"]) >= KNOWLEDGE_V2.version


def test_migrations_are_repeatable_no_ops(fresh_db):
    conn = _db()._get_connection()
    # Applying everything again must be a clean no-op (version-gated).
    apply_migrations(conn)
    apply_migrations(conn)
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'knowledge'"
    ).fetchone()
    assert int(row["version"]) == KNOWLEDGE_V2.version


def test_knowledge_v1_databases_upgrade_in_place(tmp_path):
    """A database that only ever applied KNOWLEDGE_V1 gains the claim schema."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(_MIGRATIONS_TABLE)
    for statement in KNOWLEDGE_V1.statements:
        cursor.execute(statement)
    cursor.execute(
        "INSERT OR REPLACE INTO schema_migrations (schema_name, version) "
        "VALUES (?, ?)",
        ("knowledge", KNOWLEDGE_V1.version),
    )
    # Simulate an old database where the other module migrations already ran
    # (CORE_V1 operates on the legacy core tables, which live in
    # connection.py's base schema, not in this bare fixture).
    for schema_name in ("memory", "learning", "core"):
        cursor.execute(
            "INSERT OR REPLACE INTO schema_migrations (schema_name, version) "
            "VALUES (?, ?)",
            (schema_name, 1),
        )
    conn.commit()

    apply_migrations(conn)

    tables = {
        row["name"]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "knowledge_source_claims" in tables
    columns = {
        row["name"]
        for row in cursor.execute(
            "PRAGMA table_info(knowledge_documents)"
        ).fetchall()
    }
    assert "claim_token" in columns
    row = cursor.execute(
        "SELECT version FROM schema_migrations WHERE schema_name = 'knowledge'"
    ).fetchone()
    assert int(row["version"]) == KNOWLEDGE_V2.version
    conn.close()
