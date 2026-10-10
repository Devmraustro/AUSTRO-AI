"""Zero-row updates must report failure, never success (audit finding M1).

``sources.set_state`` and ``documents.complete`` issue an UPDATE keyed by
(id, owner). When no row matches (missing record, or another owner's record)
the statement succeeds but changes nothing. These tests require the methods to
return False in that case, and the ingestion pipeline to refuse to report
completion when the final status write did not change the source row.

The store-level bodies are shared with tests/test_zero_row_updates_pg.py, which
runs them on PostgreSQL 16 by replacing only the ``manager`` fixture.
"""
from __future__ import annotations

import pytest

import database
from app.core.container import build_container
from app.knowledge.models import COMPLETED_STATUS, FAILED_STATUS
from app.knowledge.repositories import KnowledgeStore
from test_knowledge_ownership import _world

OWNER = 31
OTHER = 32
MISSING_ID = 987654


@pytest.fixture()
def manager():
    return database.db._manager


@pytest.fixture()
def store(manager):
    return KnowledgeStore(manager)


@pytest.fixture()
def a(store):
    return _world(store, OWNER, "zr-alpha")


def _scalar(manager, sql, params=()):
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return list(row.values())[0] if isinstance(row, dict) else row[0]


# --------------------------------------------------------------------------- #
# sources.set_state
# --------------------------------------------------------------------------- #
def test_set_state_on_missing_source_reports_failure(store):
    assert store.sources.set_state(OWNER, MISSING_ID, FAILED_STATUS, "EXTRACT") is False


def test_set_state_on_existing_source_succeeds(store, manager, a):
    assert store.sources.set_state(OWNER, a["source"], COMPLETED_STATUS, "READY") is True
    assert _scalar(manager, "SELECT status FROM knowledge_sources WHERE source_id = ?",
                   (a["source"],)) == COMPLETED_STATUS


def test_set_state_cross_owner_reports_failure_and_changes_nothing(store, manager, a):
    before = _scalar(manager, "SELECT status FROM knowledge_sources WHERE source_id = ?",
                     (a["source"],))
    assert store.sources.set_state(OTHER, a["source"], COMPLETED_STATUS, "READY") is False
    assert _scalar(manager, "SELECT status FROM knowledge_sources WHERE source_id = ?",
                   (a["source"],)) == before


# --------------------------------------------------------------------------- #
# documents.complete
# --------------------------------------------------------------------------- #
def test_complete_missing_document_reports_failure(store):
    assert store.documents.complete(OWNER, MISSING_ID, total_sections=3) is False


def test_complete_existing_document_succeeds(store, manager, a):
    assert store.documents.complete(OWNER, a["document"], total_sections=3) is True
    assert _scalar(manager, "SELECT total_sections FROM knowledge_documents "
                            "WHERE document_id = ?", (a["document"],)) == 3


def test_complete_cross_owner_reports_failure_and_changes_nothing(store, manager, a):
    assert store.documents.complete(OTHER, a["document"], total_sections=99) is False
    assert _scalar(manager, "SELECT total_sections FROM knowledge_documents "
                            "WHERE document_id = ?", (a["document"],)) != 99


# --------------------------------------------------------------------------- #
# ingestion: a completion write that matches no row must not report success
# --------------------------------------------------------------------------- #
@pytest.fixture()
def service():
    return build_container().knowledge


async def _register_book(service, owner: int) -> int:
    from test_knowledge_cleanup import BOOK

    upload = await service.register_upload(
        owner_user_id=owner, file_name="zr-book.txt", data=BOOK.encode("utf-8"),
        mime_type="text/plain")
    return upload["source_id"]


@pytest.mark.asyncio
async def test_ingestion_does_not_report_completion_when_final_status_write_matches_no_row(
        service, monkeypatch, manager):
    store = service._store
    source_id = await _register_book(service, OWNER)
    real_set_state = store.sources.set_state

    def row_vanishes_before_final_write(owner, sid, status, state, **kwargs):
        if status == COMPLETED_STATUS:
            # The source row disappears between the document write and the
            # final status write; the real UPDATE then matches zero rows.
            with manager._lock:
                cursor = manager._get_connection().cursor()
                cursor.execute("DELETE FROM knowledge_sources WHERE source_id = ?",
                               (sid,))
                manager._get_connection().commit()
        return real_set_state(owner, sid, status, state, **kwargs)

    monkeypatch.setattr(store.sources, "set_state", row_vanishes_before_final_write)

    result = await service.process_source(OWNER, source_id)

    assert result.status != COMPLETED_STATUS
    assert result.error
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_sources "
                            "WHERE source_id = ?", (source_id,)) == 0


@pytest.mark.asyncio
async def test_ingestion_does_not_report_completion_when_document_completion_fails(
        service, monkeypatch, manager):
    store = service._store
    source_id = await _register_book(service, OWNER)
    monkeypatch.setattr(store.documents, "complete", lambda *a, **k: False)

    result = await service.process_source(OWNER, source_id)

    assert result.status != COMPLETED_STATUS
    assert result.error
    status = _scalar(manager, "SELECT status FROM knowledge_sources WHERE source_id = ?",
                     (source_id,))
    assert status != COMPLETED_STATUS
    assert status == FAILED_STATUS
