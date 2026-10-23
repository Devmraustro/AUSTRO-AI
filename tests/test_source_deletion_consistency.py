"""Deleting a knowledge source: the file and its row must not diverge.

The stored original is removed before the row. A failed unlink must keep the
row (so the user can retry and no content is orphaned on disk). The tests use
real files and real rows, with a single injected ``unlink`` failure.
"""

from __future__ import annotations

import pathlib

import pytest

from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _count, _empty, _register


@pytest.fixture()
def service():
    return build_container().knowledge


def _stored_path(service, source_id: int) -> pathlib.Path:
    row = service._store.sources.get(OWNER, source_id)
    return service._storage._resolve(row["storage_key"])


@pytest.mark.asyncio
async def test_delete_removes_file_and_all_derived_rows(service):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    path = _stored_path(service, source_id)
    assert path.exists()

    assert service.delete_source(OWNER, source_id) is True

    assert not path.exists()
    assert service._store.sources.get(OWNER, source_id) is None
    assert _empty(_count(OWNER, source_id))


@pytest.mark.asyncio
async def test_failed_unlink_keeps_row_and_file_then_retry_succeeds(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    path = _stored_path(service, source_id)
    real_unlink = pathlib.Path.unlink

    def refuse_this_file(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated: cannot remove stored original")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refuse_this_file)
    assert service.delete_source(OWNER, source_id) is False

    assert path.exists(), "file must stay while the row exists"
    assert service._store.sources.get(OWNER, source_id) is not None
    assert _count(OWNER, source_id)["knowledge_chunks"] > 0, "index untouched"

    monkeypatch.undo()
    assert service.delete_source(OWNER, source_id) is True
    assert not path.exists()
    assert service._store.sources.get(OWNER, source_id) is None


@pytest.mark.asyncio
async def test_row_failure_after_file_removal_is_reported_and_retry_is_safe(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    path = _stored_path(service, source_id)
    monkeypatch.setattr(service._store.sources, "delete", lambda *a, **k: False)

    assert service.delete_source(OWNER, source_id) is False
    assert not path.exists(), "documented residual: file already gone"
    assert service._store.sources.get(OWNER, source_id) is not None

    monkeypatch.undo()
    assert service.delete_source(OWNER, source_id) is True
    assert service._store.sources.get(OWNER, source_id) is None
