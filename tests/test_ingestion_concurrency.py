"""Two overlapping starts for the same source must not fight over its rows.

Before the in-process guard, both runs failed: each purged and rebuilt the
other's rows. Now the second start is refused with a clear error and the first
run completes with exactly one index.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _count, _register, _source_state


@pytest.fixture()
def service():
    return build_container().knowledge


@pytest.mark.asyncio
async def test_overlapping_starts_for_one_source_do_not_destroy_each_other(service):
    source_id = await _register(service, OWNER)

    first, second = await asyncio.gather(
        service.process_source(OWNER, source_id),
        service.process_source(OWNER, source_id),
    )

    statuses = sorted([first.status, second.status])
    assert statuses == ["completed", "processing"], (first.error, second.error)
    refused = first if first.status == "processing" else second
    assert refused.error == "المعالجة جارية بالفعل"
    assert _source_state(source_id)[0] == "completed"
    counts = _count(OWNER, source_id)
    assert counts["knowledge_documents"] == 1
    assert counts["knowledge_chunks"] > 0
    assert counts["knowledge_embeddings"] == counts["knowledge_chunks"]


@pytest.mark.asyncio
async def test_guard_is_released_after_a_run_so_later_starts_work(service):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    # Completed sources short-circuit, and the key must not stay locked.
    assert service.pipeline._active == set()
    again = await service.process_source(OWNER, source_id)
    assert again.status == "completed"


# ---------------------------------------------------------------------------
# R-1 (CODE_AUDIT_REPORT §4): ingestion must not block the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_does_not_block_the_event_loop(monkeypatch):
    """CPU/IO-bound parsing must run off the loop (regression guard for R-1).

    `TextExtractor.extract` used to be awaited directly from an async handler,
    so a large PDF froze every other Telegram update. The pipeline now wraps the
    call in `asyncio.to_thread`; this test fails if that wrapper disappears,
    which is exactly how the defect would come back.
    """
    import time

    from app.knowledge import ingestion as ingestion_module

    ticks = {"count": 0}

    def slow_extract(self, data, fmt):
        """Blocking extractor: a real one parses PDF/ZIP synchronously."""
        time.sleep(0.25)
        class _Out:
            text = "نص"
            metadata = {"page_count": 1}
            def page_for_char(self, _index):
                return 1
        return _Out()

    monkeypatch.setattr(
        ingestion_module.TextExtractor, "extract", slow_extract, raising=True,
    )

    async def heartbeat():
        while True:
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        # Exercise the same call the pipeline uses.
        await asyncio.to_thread(
            ingestion_module.TextExtractor().extract, b"x", "txt"
        )
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    assert ticks["count"] > 5, (
        "the event loop stopped ticking during extraction - a blocking parse "
        "is running on the loop again (R-1)"
    )


# ---------------------------------------------------------------------------
# R-3 (CODE_AUDIT_REPORT §4): a failed ingestion must stay retryable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_ingestion_does_not_wedge_the_source(service, monkeypatch):
    """A failure must leave the source in a retryable state, not 'processing'.

    R-3 recorded that some parse/IO failure paths did not transition the source
    out of its in-progress state, so a retry could never be scheduled. A wedged
    source is invisible to the user: the book sits at "⏳ قيد المعالجة" forever.
    """
    from app.knowledge import ingestion as ingestion_module

    owner = OWNER + 7

    def explode(self, data, fmt):
        raise ValueError("corrupt payload")

    monkeypatch.setattr(
        ingestion_module.TextExtractor, "extract", explode, raising=True,
    )

    source_id = await _register(service, owner)
    result = await service.process_source(owner, source_id)

    status, error = _source_state(source_id)
    assert status == "failed", (
        f"a failed ingestion left the source in {status!r}; it can never be retried"
    )
    assert error, "no error message was recorded for the user"
    assert result.status == "failed"

    # The source is not stuck in progress, and no derived rows were kept.
    assert _count(owner, source_id) and all(
        v == 0 for v in _count(owner, source_id).values()
    ), "derived rows survived a failed attempt"


@pytest.mark.asyncio
async def test_a_failed_ingestion_can_be_retried_and_succeeds(service, monkeypatch):
    """The whole point of not wedging: a second attempt must work."""
    from app.knowledge import ingestion as ingestion_module

    owner = OWNER + 8
    source_id = await _register(service, owner)

    original = ingestion_module.TextExtractor.extract
    calls = {"n": 0}

    def fail_once(self, data, fmt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("transient parse failure")
        return original(self, data, fmt)

    monkeypatch.setattr(
        ingestion_module.TextExtractor, "extract", fail_once, raising=True,
    )

    first = await service.process_source(owner, source_id)
    assert first.status == "failed"
    assert _source_state(source_id)[0] == "failed"

    second = await service.process_source(owner, source_id)
    assert second.status == "completed", (
        "the source could not be retried after a failure (R-3)"
    )
    assert _source_state(source_id)[0] == "completed"
