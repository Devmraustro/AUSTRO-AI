"""Cross-owner access attempts and deletion edge cases for personal memory.

Owner A (1) creates memories; owner B (2) must never be able to read, list,
search, update, version, event-log, export, count, forget, clear or delete them.
Every call is expected to be a no-op from B's side and must leave A's data
untouched. Deletion edge cases (missing ids, repeated deletes, empty clears)
are covered here as well.
"""

from __future__ import annotations

import pytest

from app.memory.models import MemoryCandidate


@pytest.fixture()
def memory():
    from app.core.container import build_container

    return build_container().memory


def _make(memory, owner: int = 1, text: str = "اسمي أحمد") -> int:
    memory.process_message(owner, text)
    items = memory.list(owner)
    assert items, "fixture failed to create a memory"
    return items[0]["memory_id"]


def test_cross_user_get_is_denied(memory):
    mid = _make(memory, 1)
    assert memory.get(2, mid) is None
    # A's data is untouched.
    assert memory.get(1, mid) is not None


def test_cross_user_list_and_search_are_empty(memory):
    _make(memory, 1)
    assert memory.list(2) == []
    assert memory.search(2, "أحمد") == []


def test_cross_user_counts_and_events_are_empty(memory):
    _make(memory, 1)
    assert memory.counts(2).get("total", 0) == 0
    assert memory.events(2) == []
    assert memory.versions(2, 1) == []


def test_cross_user_update_forget_delete_clear_are_no_ops(memory):
    mid = _make(memory, 1)
    before = memory.get(1, mid)

    assert memory.edit(2, mid, "改 claim") is False
    assert memory.forget(2, mid) is False
    assert memory.delete(2, mid) is False
    assert memory.clear(2) == 0

    # Nothing changed for owner A.
    after = memory.get(1, mid)
    assert after is not None
    assert after["claim"] == before["claim"]
    assert after["memory_id"] == mid


def test_cross_user_export_does_not_leak(memory):
    _make(memory, 1)
    payload = memory.export(2)
    # No active memories for B -> export is None (no leak).
    assert payload is None
    # A still has the memory.
    assert memory.export(1)["memory_count"] == 1


def test_forget_missing_and_repeated(memory):
    assert memory.forget(1, 999999) is False
    mid = _make(memory, 1)
    assert memory.forget(1, mid) is True
    assert all(m["memory_id"] != mid for m in memory.list(1))
    # Re-forgetting the tombstone does not resurrect it.
    assert memory.forget(1, mid) is True
    assert all(m["memory_id"] != mid for m in memory.list(1))


def test_delete_missing_and_repeated(memory):
    assert memory.delete(1, 999999) is False
    mid = _make(memory, 1)
    assert memory.delete(1, mid) is True
    assert memory.delete(1, mid) is False
    assert memory.get(1, mid) is None


def test_clear_empty_and_repeated(memory):
    assert memory.clear(1) == 0
    _make(memory, 1)
    _make(memory, 1, "أحب القهوة")
    assert memory.clear(1) >= 1
    assert memory.list(1) == []
    # Clearing again leaves no active memories.
    assert memory.clear(1) >= 0
    assert memory.list(1) == []


def test_cross_user_confirmed_and_pending_are_scoped(memory):
    memory.process_candidates(1, [MemoryCandidate(
        owner_user_id=1, memory_type="preference", subject="القهوة",
        claim="يفضل القهوة العربية", confidence="high",
        provenance="explicit_user_statement",
    )])
    # Pending list for B must be empty even though A has an item.
    # (process_candidates with high confidence auto-creates, so check counts.)
    assert memory.counts(2).get("total", 0) == 0
    assert memory.pending(2) == []
    # Rejecting a knowledge-origin fact for B must not affect A.
    assert memory.reject_knowledge_origin(2, "يفضل المستخدم الشاي") is True
    assert memory.counts(1).get("total", 0) >= 1
