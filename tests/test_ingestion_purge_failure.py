"""Ingestion cleanup when the purge itself fails.

A SQLite trigger makes ``purge_partial_index`` fail (the DELETE on
``knowledge_sections`` aborts, so the purge transaction rolls back and the
leftovers stay). The tests check, with raw SQL and the real retrieval
candidate query: what remains, whether search can see it, whether the failure
is logged, and whether a retry recovers without manual database edits.
"""

from __future__ import annotations

import logging

import pytest

import database
from app.core.container import build_container
from tests.test_knowledge_cleanup import (
    OWNER, _count, _empty, _register, _source_state)

TRIGGER = "fail_purge_sections"


@pytest.fixture()
def service():
    return build_container().knowledge


def _exec(sql: str) -> None:
    manager = database.db._manager
    with manager._lock:
        conn = manager._get_connection()
        conn.cursor().execute(sql)
        conn.commit()


@pytest.fixture()
def purge_broken():
    _exec(f"CREATE TRIGGER {TRIGGER} BEFORE DELETE ON knowledge_sections "
          "BEGIN SELECT RAISE(ABORT, 'simulated purge failure'); END")
    yield
    _exec(f"DROP TRIGGER IF EXISTS {TRIGGER}")


async def _fail_late_with_broken_purge(service, monkeypatch) -> int:
    source_id = await _register(service, OWNER)
    monkeypatch.setattr(service.pipeline._store.embeddings, "count_for_source",
                        lambda *a, **k: -1)
    result = await service.process_source(OWNER, source_id)
    assert result.status == "failed"
    return source_id


@pytest.mark.asyncio
async def test_failed_purge_leaves_rows_that_search_cannot_see_and_logs_it(
        service, monkeypatch, purge_broken, caplog):
    caplog.set_level(logging.ERROR)
    source_id = await _fail_late_with_broken_purge(service, monkeypatch)

    leftovers = _count(OWNER, source_id)
    assert leftovers["knowledge_documents"] == 1, "purge rollback keeps the leftovers"
    assert leftovers["knowledge_chunks"] > 0
    assert _source_state(source_id)[0] == "failed"
    # Not searchable: retrieval only reads chunks of COMPLETED sources.
    assert service.pipeline._store.chunks.list_candidates(OWNER) == []
    # Observable: the purge failure is logged, not silently swallowed.
    assert any("purging partial knowledge index" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)


@pytest.mark.asyncio
async def test_retry_after_failed_purge_recovers_on_first_retry(
        service, monkeypatch, purge_broken):
    source_id = await _fail_late_with_broken_purge(service, monkeypatch)
    assert not _empty(_count(OWNER, source_id))

    _exec(f"DROP TRIGGER {TRIGGER}")  # the fault is gone; nothing edited by hand
    monkeypatch.undo()
    retry = await service.process_source(OWNER, source_id)

    assert retry.status == "completed", retry.error
    counts = _count(OWNER, source_id)
    assert counts["knowledge_documents"] == 1, "no duplicate document from the leftovers"
    assert counts["knowledge_chunks"] > 0
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]


@pytest.mark.asyncio
async def test_retry_refuses_to_rebuild_while_leftovers_cannot_be_removed(
        service, monkeypatch, purge_broken):
    source_id = await _fail_late_with_broken_purge(service, monkeypatch)
    before = _count(OWNER, source_id)
    monkeypatch.undo()  # verification is real again, but the purge is still broken

    retry = await service.process_source(OWNER, source_id)

    assert retry.status == "failed"
    assert retry.error and "تنظيف" in retry.error
    assert _count(OWNER, source_id) == before, "refused retry must not change rows"
    status, error = _source_state(source_id)
    assert status == "failed" and error == retry.error

    _exec(f"DROP TRIGGER {TRIGGER}")
    final = await service.process_source(OWNER, source_id)
    assert final.status == "completed", final.error
