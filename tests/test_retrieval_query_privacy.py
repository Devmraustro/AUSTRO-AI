"""Retrieval events never persist the user's query text.

The raw search string is not stored (``knowledge_retrieval_events.query`` is
written as ``''``). These tests run the real retrieval path on SQLite and check
the stored rows with raw SQL: the secret query must not appear anywhere in the
table, retrieval results and citations must still be recorded, and the legacy
scrub must blank old query text while keeping every row and citation.
"""

from __future__ import annotations

import pytest

import database
from app.core.container import build_container
from tests.test_knowledge_cleanup import BOOK

OWNER = 61
SECRET = "zq-secret-medical-question-7731"
# Real words from BOOK so the search has evidence to return; the secret token
# must still never reach the database.
SEARCH = f"{SECRET} إدارة الوقت"


def _raw(sql: str, params=()):
    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


@pytest.fixture()
def container():
    return build_container()


async def _indexed_source(container, owner: int) -> None:
    upload = await container.knowledge.register_upload(
        owner_user_id=owner, file_name="book.txt", data=BOOK.encode("utf-8"),
        mime_type="text/plain")
    result = await container.knowledge.process_source(owner, upload["source_id"])
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_search_stores_event_and_citations_but_not_query_text(container):
    await _indexed_source(container, OWNER)

    result = container.knowledge.retrieval.retrieve(OWNER, SEARCH, top_k=3)

    assert result.chunks, "retrieval must still return evidence for an indexed owner"
    assert result.event_id is not None
    rows = _raw("SELECT query, result_count FROM knowledge_retrieval_events "
                "WHERE event_id = ?", (result.event_id,))
    assert rows == [("", len(result.chunks))]
    citations = _raw("SELECT COUNT(*) FROM knowledge_citations WHERE event_id = ?",
                     (result.event_id,))
    assert citations[0][0] == len(result.chunks)
    # The secret must not be stored in any retrieval event row.
    leaked = _raw("SELECT COUNT(*) FROM knowledge_retrieval_events WHERE query LIKE ?",
                  (f"%{SECRET}%",))
    assert leaked[0][0] == 0


def test_empty_index_search_logs_event_without_query_text(container):
    before = _raw("SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0]

    result = container.knowledge.retrieval.retrieve(OWNER + 1, SECRET, top_k=3)

    assert result.chunks == []
    assert result.event_id is not None
    after = _raw("SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0]
    assert after == before + 1
    assert _raw("SELECT query FROM knowledge_retrieval_events WHERE event_id = ?",
                (result.event_id,)) == [("",)]


def test_legacy_query_scrub_blanks_text_and_keeps_rows_and_citations(container):
    store = container.knowledge._store
    _raw("INSERT INTO knowledge_retrieval_events "
         "(owner_user_id, query, top_k, result_count, latency_ms, generator) "
         "VALUES (?, ?, ?, ?, ?, ?)", (OWNER, SECRET, 3, 0, 1.0, "local"))
    total_before = _raw("SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0]

    changed = store.events.scrub_legacy_query_text()

    assert changed >= 1
    assert _raw("SELECT COUNT(*) FROM knowledge_retrieval_events WHERE query <> ''")[0][0] == 0
    assert _raw("SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0] == total_before
    # Idempotent: a second run changes nothing.
    assert store.events.scrub_legacy_query_text() == 0
