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
