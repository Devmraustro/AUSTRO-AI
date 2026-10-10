"""Chunks are linked to the section they were extracted from.

Root cause (chunker._section_at): the chunk took the matched section's PARENT
reference, so every chunk in a top-level section had section_id NULL. Retrieval
then reported section_title None on citations. These tests check the real
linkage: the stored section row must be the chunk's own section, and retrieval
must report its title.
"""

from __future__ import annotations

import database
import pytest

from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _register
from tests.test_retrieval_query_privacy import SEARCH


def _linked_rows(source_id: int):
    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute(
            "SELECT c.chunk_id, c.section_id, s.title AS section_row_title, c.metadata_json "
            "FROM knowledge_chunks c LEFT JOIN knowledge_sections s "
            "ON s.section_id = c.section_id AND s.owner_user_id = c.owner_user_id "
            "WHERE c.source_id = ?", (source_id,))
        rows = cursor.fetchall()
    return [dict(r) if isinstance(r, dict) else dict(zip(
        ("chunk_id", "section_id", "section_row_title", "metadata_json"), r)) for r in rows]


@pytest.fixture()
def service():
    return build_container().knowledge


@pytest.mark.asyncio
async def test_every_chunk_is_linked_to_its_own_section(service):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    rows = _linked_rows(source_id)
    assert rows
    for row in rows:
        assert row["section_id"] is not None, "chunk must keep its section link"
        assert row["section_row_title"] is not None, "linked section must exist for this owner"
        import json
        meta = json.loads(row["metadata_json"] or "{}")
        assert row["section_row_title"] == meta.get("section_title"), (
            "section link must point at the section the chunk came from")


@pytest.mark.asyncio
async def test_retrieval_citation_reports_the_section_title(service):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    result = service.retrieval.retrieve(OWNER, SEARCH, top_k=3)
    assert result.chunks
    assert all(chunk.section_title for chunk in result.chunks)


NESTED_DOC = (
    "Chapter 1 Basics\n\n"
    + "Time management starts with clear priorities and a daily plan for the student. " * 6
    + "\n\nSection 1.1 Planning\n\n"
    + "Daily planning orders tasks so that the important work is done first each day. " * 6
    + "\n\nSection 1.2 Review\n\n"
    + "A weekly review shows progress and lets the plan change when needed over time. " * 6
    + "\n\nChapter 2 Habits\n\n"
    + "Small habits build discipline slowly and make a large difference in the end. " * 6
)


@pytest.mark.asyncio
async def test_chunk_in_a_subsection_links_to_that_subsection_not_its_chapter():
    """A chunk belongs to its most specific (innermost) section.

    _section_at returned the first containing section, which for a nested
    layout is the chapter whose range covers its subsections.
    """
    service = build_container().knowledge
    upload = await service.register_upload(
        owner_user_id=OWNER, file_name="nested.txt", data=NESTED_DOC.encode("utf-8"),
        mime_type="text/plain")
    assert (await service.process_source(OWNER, upload["source_id"])).status == "completed"

    expected = {
        "Daily planning": "Section 1.1 Planning",
        "A weekly review": "Section 1.2 Review",
        "Chapter 1 Basics": "Chapter 1 Basics",
        "Small habits": "Chapter 2 Habits",
    }
    rows = _linked_rows(upload["source_id"])
    assert rows
    checked = set()
    for row in rows:
        content_head = row["section_row_title"]  # linked section title
        for marker, title in expected.items():
            if _chunk_text(row["chunk_id"]).startswith(marker):
                checked.add(marker)
                assert content_head == title, f"{marker!r} linked to {content_head!r}"
    assert checked == set(expected), "every nested chunk must be verified"


def _chunk_text(chunk_id: int) -> str:
    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute("SELECT content FROM knowledge_chunks WHERE chunk_id = ?", (chunk_id,))
        row = cursor.fetchone()
    return (list(row.values())[0] if isinstance(row, dict) else row[0]) or ""
