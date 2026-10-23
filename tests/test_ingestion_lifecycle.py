"""Ingestion lifecycle: no stuck states, no false success.

Covers three paths that used to leave the source in a state a single retry
could not repair, or report success that was not persisted:
  * cancellation mid-index (asyncio.CancelledError is a BaseException);
  * a hard crash (a non-Exception BaseException that skips every handler),
    which leaves the source PROCESSING with partial rows;
  * the COMPLETED status write not persisting.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _count, _empty, _register, _source_state


class SimulatedCrash(BaseException):
    """Stands in for a process kill: skips every `except Exception`."""


@pytest.fixture()
def service():
    return build_container().knowledge


def _break_index(service, monkeypatch, exc: BaseException) -> None:
    pipeline = service.pipeline
    real = pipeline._index_chunks

    def index_then_break(owner, src, document_id, section_refs, chunks, vectors,
                         claim_token=None):
        # The claim-based pipeline threads claim_token through every write;
        # index exactly one chunk under it, then fail at the same seam.
        real(owner, src, document_id, section_refs, chunks[:1], vectors[:1],
             claim_token)
        raise exc

    monkeypatch.setattr(pipeline, "_index_chunks", index_then_break)


@pytest.mark.asyncio
async def test_cancelled_run_is_failed_purged_and_first_retry_completes(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    _break_index(service, monkeypatch, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await service.process_source(OWNER, source_id)

    status, error = _source_state(source_id)
    assert status == "failed" and error, "cancelled run must not stay PROCESSING"
    assert _empty(_count(OWNER, source_id)), "partial rows removed on cancellation"

    monkeypatch.undo()
    retry = await service.process_source(OWNER, source_id)
    assert retry.status == "completed", retry.error


@pytest.mark.asyncio
async def test_crashed_run_leaves_processing_but_next_attempt_cleans_it(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    _break_index(service, monkeypatch, SimulatedCrash())

    with pytest.raises(SimulatedCrash):
        await service.process_source(OWNER, source_id)
    assert _source_state(source_id)[0] == "processing"
    assert not _empty(_count(OWNER, source_id)), "the crash left partial rows"

    monkeypatch.undo()
    retry = await service.process_source(OWNER, source_id)
    assert retry.status == "completed", retry.error
    assert _count(OWNER, source_id)["knowledge_documents"] == 1


@pytest.mark.asyncio
async def test_unpersisted_completed_status_is_not_reported_as_success(
        service, monkeypatch):
    source_id = await _register(service, OWNER)
    store = service.pipeline._store

    def refuse_completed(**kwargs):
        # Publication is atomic in the claim-based pipeline: a source row
        # that vanished (or a claim that was lost) makes publish raise
        # ClaimLostError and roll back. That is the current equivalent of
        # the old zero-row COMPLETED write.
        from app.core.errors import ClaimLostError
        raise ClaimLostError("simulated: completion did not persist")

    monkeypatch.setattr(store.claims, "publish", refuse_completed)
    result = await service.process_source(OWNER, source_id)

    assert result.status == "failed", "a write that did not persist is not success"
    assert _source_state(source_id)[0] != "completed"
    assert _empty(_count(OWNER, source_id))
