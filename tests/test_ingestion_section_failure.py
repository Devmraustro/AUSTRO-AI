"""A failed section insert must fail the ingestion, never complete it.

Before the fix, a section insert that returned None was stored as section 0,
so its chunks were written with no section and the source was marked
COMPLETED. Now the run fails, the partial index is purged, and a retry builds
a complete, sectioned index.
"""

from __future__ import annotations

import database
import pytest

from tests.test_knowledge_cleanup import OWNER, _count, _register, _source_state
from app.core.container import build_container


@pytest.fixture()
def service():
    return build_container().knowledge


def _sectionless_chunks(source_id: int) -> int:
    manager = database.db._manager
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute("SELECT COUNT(*) FROM knowledge_chunks "
                       "WHERE source_id = ? AND section_id IS NULL", (source_id,))
        row = cursor.fetchone()
    return list(row.values())[0] if isinstance(row, dict) else row[0]


@pytest.mark.asyncio
async def test_failed_section_insert_fails_ingestion_and_retry_is_sectioned(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    store = service.pipeline._store
    real_create = store.sections.create
    calls = {"n": 0}

    def fail_first_section(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_create(*args, **kwargs)

    monkeypatch.setattr(store.sections, "create", fail_first_section)
    result = await service.process_source(OWNER, source_id)

    assert result.status == "failed", "must not complete with a missing section"
    assert _source_state(source_id)[0] == "failed"
    assert all(v == 0 for v in _count(OWNER, source_id).values()), "partial index purged"

    monkeypatch.undo()
    retry = await service.process_source(OWNER, source_id)
    assert retry.status == "completed", retry.error
    assert _sectionless_chunks(source_id) == 0
    assert _count(OWNER, source_id)["knowledge_chunks"] > 0
