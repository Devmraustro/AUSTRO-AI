"""Identical paragraphs must not block indexing.

Regression for knowledge_chunks UNIQUE(owner_user_id, chunk_key): the key was the
bare content hash, so a paragraph repeated inside one book, or shared by two
books of the same owner, made the insert collide and the source failed forever.
"""

from __future__ import annotations

import pytest

from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _count, _source_state

PARA_A = ("إدارة الوقت مهارة أساسية تساعد الإنسان على تنظيم يومه. " * 14).strip()
PARA_B = ("التخطيط الأسبوعي يمنح الفرد رؤية واضحة لأولوياته وأهدافه. " * 14).strip()
PARA_C = ("الانضباط الذاتي يبني العادات الجيدة مع مرور الأيام. " * 14).strip()


def _book(*paragraphs: str) -> bytes:
    # No heading: a heading merged into the first paragraph would make that
    # chunk's text differ, so identical paragraphs would not collide.
    return ("\n\n".join(paragraphs) + "\n").encode("utf-8")


async def _upload_and_process(service, name: str, data: bytes):
    upload = await service.register_upload(
        owner_user_id=OWNER, file_name=name, data=data, mime_type="text/plain")
    result = await service.process_source(OWNER, upload["source_id"])
    return upload["source_id"], result


@pytest.fixture()
def service():
    return build_container().knowledge


@pytest.mark.asyncio
async def test_paragraph_repeated_inside_one_book_is_indexed(service):
    source_id, result = await _upload_and_process(
        service, "repeats.txt", _book(PARA_A, PARA_B, PARA_A))

    assert result.status == "completed", result.error
    counts = _count(OWNER, source_id)
    assert counts["knowledge_chunks"] >= 2
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]
    assert _source_state(source_id)[0] == "completed"


@pytest.mark.asyncio
async def test_paragraph_shared_by_two_books_indexes_both_and_survives_delete(service):
    first_id, first = await _upload_and_process(service, "one.txt", _book(PARA_A, PARA_B))
    second_id, second = await _upload_and_process(service, "two.txt", _book(PARA_C, PARA_A))

    assert first.status == "completed", first.error
    assert second.status == "completed", second.error

    second_counts = _count(OWNER, second_id)
    assert second_counts["knowledge_chunks"] >= 2
    assert second_counts["knowledge_embeddings"] == second_counts["knowledge_chunks"]

    assert service.delete_source(OWNER, first_id)

    # Book two keeps its own copy of the shared paragraph after book one is gone.
    after = _count(OWNER, second_id)
    assert after["knowledge_chunks"] == second_counts["knowledge_chunks"]
    assert after["knowledge_embeddings"] == second_counts["knowledge_embeddings"]


@pytest.mark.asyncio
async def test_retrieval_still_shows_identical_text_once(service):
    await _upload_and_process(service, "p.txt", _book(PARA_A, PARA_B))
    await _upload_and_process(service, "q.txt", _book(PARA_A, PARA_C))

    result = service.search(OWNER, "إدارة الوقت مهارة أساسية")
    contents = [chunk.content for chunk in result.chunks]
    shared_hits = [c for c in contents if "إدارة الوقت مهارة أساسية" in c]
    assert shared_hits, "the shared paragraph must be retrievable"
    assert len(shared_hits) == 1, "identical text from two books shown once"


@pytest.mark.asyncio
async def test_higher_scored_copy_replaces_lower_scored_identical_text(service):
    """The better-matching copy must be kept, not both (dedupe must replace).

    Candidates are ordered newest first. The older book's title matches the
    query, so its copy scores higher but is processed second.
    """
    import database

    older_id, older = await _upload_and_process(service, "older.txt", _book(PARA_A, PARA_B))
    newer_id, newer = await _upload_and_process(service, "newer.txt", _book(PARA_A, PARA_C))
    assert older.status == "completed" and newer.status == "completed"

    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute("UPDATE knowledge_sources SET title = ? WHERE source_id = ?",
                       ("إدارة الوقت مهارة أساسية", older_id))
        # Make the newer book's chunks sort first (created_at DESC), so the
        # lower-scored copy is processed before the higher-scored one.
        cursor.execute("UPDATE knowledge_chunks SET created_at = '2999-01-01 00:00:00' "
                       "WHERE source_id = ?", (newer_id,))
        manager._get_connection().commit()

    candidates = service.retrieval._store.chunks.list_candidates(OWNER)
    shared_order = [c["source_id"] for c in candidates if PARA_A[:40] in (c["content"] or "")]
    assert shared_order[:1] == [newer_id], "precondition: lower-scored copy processed first"

    result = service.search(OWNER, "إدارة الوقت مهارة أساسية")
    shared = [c for c in result.chunks if PARA_A[:40] in c.content]
    assert len(shared) == 1, "identical text from two books must be shown once"
    assert shared[0].source_id == older_id, "the higher-scored copy must be kept"
