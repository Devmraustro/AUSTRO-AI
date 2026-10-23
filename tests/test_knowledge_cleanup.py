"""Ingestion failure cleanup: late-stage failures leave no orphaned index rows.

Each test drives the real IngestionPipeline on SQLite, forces a failure at a
late stage (after sections, chunks and embeddings were written), then asserts
on the stored values with raw SQL: no document, section, chunk or embedding of
the failed source remains, the source is FAILED with its error, another owner's
rows are untouched, and a retry rebuilds exactly one clean index.
"""

from __future__ import annotations

import pytest

import database
from app.core.container import build_container

OWNER = 21
OTHER_OWNER = 22
BOOK = (
    "الفصل الأول\n"
    "سر إدارة الوقت\n\n"
    "إدارة الوقت هي المهارة الأساسية التي تسمح للإنسان بإنجاز أهدافه اليومية بكفاءة.\n"
    "تبدأ الإدارة الجيدة بتحديد الأولويات وكتابة الخطة اليومية قبل بداية العمل.\n\n"
    "الفصل الثاني\n"
    "أدوات التخطيط\n\n"
    "المفكرة الورقية والتطبيقات الرقمية كلتاهما تساعد على متابعة المهام المهمة.\n"
)
DERIVED_TABLES = ("knowledge_embeddings", "knowledge_chunks",
                  "knowledge_sections", "knowledge_documents")


def _count(owner: int, source_id: int) -> dict:
    manager = database.db._manager
    counts = {}
    with manager._lock:
        cursor = manager._get_connection().cursor()
        for table in DERIVED_TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table} "  # nosec B608 - fixed names
                           "WHERE owner_user_id = ? AND source_id = ?",
                           (owner, source_id))
            counts[table] = cursor.fetchone()[0]
    return counts


def _source_state(source_id: int):
    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute("SELECT status, error_message FROM knowledge_sources "
                       "WHERE source_id = ?", (source_id,))
        row = cursor.fetchone()
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


@pytest.fixture()
def service():
    return build_container().knowledge


async def _register(service, owner: int) -> int:
    upload = await service.register_upload(
        owner_user_id=owner, file_name="book.txt", data=BOOK.encode("utf-8"),
        mime_type="text/plain")
    return upload["source_id"]


def _empty(counts: dict) -> bool:
    return all(v == 0 for v in counts.values())


@pytest.mark.asyncio
async def test_late_index_failure_removes_partial_rows(service, monkeypatch):
    source_id = await _register(service, OWNER)
    pipeline = service.pipeline
    real_index_chunks = pipeline._index_chunks

    def fail_after_first_chunk(owner, src, document_id, section_refs, chunks, vectors):
        # Write the first chunk and its embedding for real, then fail mid-INDEX.
        real_index_chunks(owner, src, document_id, section_refs,
                          chunks[:1], vectors[:1])
        raise RuntimeError("simulated crash while indexing chunk 2")

    monkeypatch.setattr(pipeline, "_index_chunks", fail_after_first_chunk)
    result = await service.process_source(OWNER, source_id)

    assert result.status == "failed"
    assert _empty(_count(OWNER, source_id)), "partial index must be removed"
    status, error = _source_state(source_id)
    assert status == "failed" and error


@pytest.mark.asyncio
async def test_failure_at_verification_stage_removes_all_rows(service, monkeypatch):
    source_id = await _register(service, OWNER)
    # Everything (sections, chunks, embeddings) is written; the final
    # "is it searchable?" count then disagrees, so ingestion must fail.
    monkeypatch.setattr(service.pipeline._store.embeddings, "count_for_source",
                        lambda *a, **k: -1)
    result = await service.process_source(OWNER, source_id)

    assert result.status == "failed"
    assert _empty(_count(OWNER, source_id))
    assert _source_state(source_id)[0] == "failed"


@pytest.mark.asyncio
async def test_retry_after_late_failure_builds_one_clean_index(service, monkeypatch):
    source_id = await _register(service, OWNER)
    monkeypatch.setattr(service.pipeline._store.embeddings, "count_for_source",
                        lambda *a, **k: -1)
    assert (await service.process_source(OWNER, source_id)).status == "failed"
    assert _empty(_count(OWNER, source_id))

    monkeypatch.undo()  # restore the real verification
    retry = await service.process_source(OWNER, source_id)
    assert retry.status == "completed"
    counts = _count(OWNER, source_id)
    assert counts["knowledge_documents"] == 1, "retry must not leave a second document"
    assert counts["knowledge_chunks"] > 0
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]


@pytest.mark.asyncio
async def test_cleanup_never_touches_another_owners_rows(service, monkeypatch):
    other_source = await _register(service, OTHER_OWNER)
    assert (await service.process_source(OTHER_OWNER, other_source)).status == "completed"
    before = _count(OTHER_OWNER, other_source)
    assert before["knowledge_chunks"] > 0

    source_id = await _register(service, OWNER)
    monkeypatch.setattr(service.pipeline._store.embeddings, "count_for_source",
                        lambda *a, **k: -1)
    assert (await service.process_source(OWNER, source_id)).status == "failed"

    assert _count(OTHER_OWNER, other_source) == before
    assert _source_state(other_source)[0] == "completed"


@pytest.mark.asyncio
async def test_failed_purge_is_reported_without_masking_the_ingestion_error(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    store = service.pipeline._store
    monkeypatch.setattr(store.embeddings, "count_for_source", lambda *a, **k: -1)
    monkeypatch.setattr(store, "purge_partial_index", lambda *a, **k: False)
    result = await service.process_source(OWNER, source_id)
    # The original ingestion failure is what the caller sees, unchanged.
    assert result.status == "failed"
    assert result.error and "فهرسة" in result.error


def test_purge_is_a_noop_for_a_source_with_no_index(service):
    store = service.pipeline._store
    assert store.purge_partial_index(OWNER, 987654) is True
