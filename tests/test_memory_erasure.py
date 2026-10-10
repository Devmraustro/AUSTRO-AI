"""Memory erasure: forget / clear / hard delete leave no sensitive plaintext.

Every assertion inspects the stored database values DIRECTLY (raw SQL over every
memory table, every text column), not the service's read API, because the
regression being guarded is "the row still contains the text", which a
filtered read can hide.
"""

from __future__ import annotations

import pytest

from app.core.container import build_container
from app.memory.models import MemoryItem, build_hash_key
from app.memory.repositories import ERASED_PLACEHOLDER, EraseFailed

SECRET = "Pluto-Kestrel-7731 secret medical diagnosis"
SECRET_SUBJECT = "Kestrel-Clinic"
SECRET_NOTE = "user disclosed Kestrel-Clinic visit"
SECRET_META = "Kestrel-metadata-token"
OTHER_CLAIM = "prefers green tea in the morning"
MEMORY_TABLES = ("memories", "memory_versions", "memory_events",
                 "memory_access_log", "memory_prefs")


def _raw(memory, sql: str, params=()):
    manager = memory.store.memories._manager
    with manager._lock:
        cursor = memory.store.memories._connection().cursor()
        cursor.execute(sql, params)
        # SQLite returns sqlite3.Row, PostgreSQL the dialect's dict rows: take values.
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


def _all_text_cells(memory):
    """Every value of every row in every memory table, as strings."""
    cells = []
    for table in MEMORY_TABLES:
        for row in _raw(memory, f"SELECT * FROM {table}"):
            cells.extend(str(v) for v in row if v is not None)
    return cells


def _seed(memory, owner: int, claim: str, *, memory_type="profile",
          subject="", source_note="", metadata=None, status="active", tag=""):
    """Persist a real memory row plus a version and an audit event, as the app does."""
    key = build_hash_key(owner, "USER", memory_type, subject, claim)
    memory_id = memory.store.memories.create(MemoryItem(
        owner_user_id=owner, scope="USER", memory_type=memory_type,
        subject=subject, claim=claim, confidence="high", importance=3,
        provenance="user_statement", source_note=source_note,
        metadata=metadata or {}, status=status, hash_key=key,
        event_date="2025-01-01" if memory_type == "event" else None,
    ))
    assert memory_id, "seed insert must succeed"
    memory.store.versions.add(
        memory_id=memory_id, owner_user_id=owner, version=1, claim=claim,
        confidence="high", importance=3, reason="seed", actor=owner)
    memory.store.events.log(owner_user_id=owner, memory_id=memory_id,
                            action="created", source="test", actor=owner)
    memory.store.access.log(owner_user_id=owner, memory_id=memory_id,
                            purpose="context_pack")
    return memory_id


@pytest.fixture()
def memory():
    return build_container().memory


@pytest.fixture()
def seeded(memory):
    secret_id = _seed(memory, 1, SECRET, memory_type="profile",
                      subject=SECRET_SUBJECT, source_note=SECRET_NOTE,
                      metadata={"detail": SECRET_META})
    kept_id = _seed(memory, 1, OTHER_CLAIM, memory_type="preference")
    return memory, secret_id, kept_id


# ------------------------------------------------------------------ forget --- #
def test_forget_scrubs_every_content_column_of_the_row(seeded):
    memory, secret_id, _ = seeded
    assert memory.forget(1, secret_id) is True
    row = _raw(memory, "SELECT status, claim, subject, source_note, metadata_json, "
                       "event_date, hash_key FROM memories WHERE memory_id = ?",
               (secret_id,))[0]
    status, claim, subject, source_note, metadata_json, event_date, hash_key = row
    assert status == "forgotten"
    assert claim == ERASED_PLACEHOLDER
    assert subject == "" and source_note == "" and metadata_json is None
    assert event_date is None
    assert hash_key == f"erased-{secret_id}"  # tombstone, not the content hash


def test_forget_leaves_no_secret_in_any_memory_table(seeded):
    memory, secret_id, _ = seeded
    memory.forget(1, secret_id)
    for cell in _all_text_cells(memory):
        assert SECRET not in cell
        assert SECRET_SUBJECT not in cell
        assert SECRET_NOTE not in cell
        assert SECRET_META not in cell


def test_forget_redacts_version_history_of_that_memory_only(seeded):
    memory, secret_id, kept_id = seeded
    memory.forget(1, secret_id)
    secret_versions = _raw(memory, "SELECT claim FROM memory_versions WHERE memory_id = ?",
                           (secret_id,))
    assert secret_versions and all(v[0] == ERASED_PLACEHOLDER for v in secret_versions)
    kept_versions = _raw(memory, "SELECT claim FROM memory_versions WHERE memory_id = ?",
                         (kept_id,))
    assert kept_versions and all(v[0] == OTHER_CLAIM for v in kept_versions)


def test_forget_does_not_touch_an_unrelated_memory(seeded):
    memory, secret_id, kept_id = seeded
    before = _raw(memory, "SELECT claim, status, subject, source_note, hash_key "
                          "FROM memories WHERE memory_id = ?", (kept_id,))
    memory.forget(1, secret_id)
    after = _raw(memory, "SELECT claim, status, subject, source_note, hash_key "
                         "FROM memories WHERE memory_id = ?", (kept_id,))
    assert before == after and after[0][0] == OTHER_CLAIM


def test_forget_writes_a_content_free_audit_event(seeded):
    memory, secret_id, _ = seeded
    memory.forget(1, secret_id)
    events = _raw(memory, "SELECT action, source, reason FROM memory_events "
                          "WHERE memory_id = ? AND action = 'forgotten'", (secret_id,))
    assert events == [("forgotten", "user", "")]


def test_forget_of_a_foreign_owners_memory_changes_nothing(seeded):
    memory, secret_id, _ = seeded
    assert memory.forget(2, secret_id) is False
    claim = _raw(memory, "SELECT claim, status FROM memories WHERE memory_id = ?",
                 (secret_id,))[0]
    assert claim == (SECRET, "active")


def test_forget_of_a_missing_memory_reports_false(memory):
    assert memory.forget(1, 999999) is False


# ------------------------------------------------------------ relearn / dedup --- #
def test_relearning_a_forgotten_fact_creates_a_new_row_without_resurrection(memory):
    text = "أفضل القهوة العربية"
    memory.process_message(1, text)
    before = [m["memory_id"] for m in memory.list(1)]
    assert before, "fact must be stored first"
    old_id = before[0]
    assert memory.forget(1, old_id) is True

    memory.process_message(1, text)
    active = memory.list(1)
    assert len(active) == 1
    new_id = active[0]["memory_id"]
    assert new_id != old_id, "the forgotten row must never be reactivated"
    assert _raw(memory, "SELECT status, claim FROM memories WHERE memory_id = ?",
                (old_id,))[0] == ("forgotten", ERASED_PLACEHOLDER)
    # The erased row and everything that references it hold no plaintext, even
    # after the fact was relearned. (The NEW active row legitimately holds it.)
    old_cells = []
    for sql in ("SELECT * FROM memories WHERE memory_id = ?",
                "SELECT * FROM memory_versions WHERE memory_id = ?",
                "SELECT * FROM memory_events WHERE memory_id = ?",
                "SELECT * FROM memory_access_log WHERE memory_id = ?"):
        for row in _raw(memory, sql, (old_id,)):
            old_cells.extend(str(v) for v in row if v is not None)
    assert old_cells and all(text not in cell for cell in old_cells)


def test_set_status_cannot_forget_without_erasing(seeded):
    memory, secret_id, _ = seeded
    with pytest.raises(ValueError):
        memory.store.memories.set_status(1, secret_id, "forgotten")
    assert _raw(memory, "SELECT claim FROM memories WHERE memory_id = ?",
                (secret_id,))[0][0] == SECRET


# -------------------------------------------------------------------- clear --- #
def test_typed_clear_only_affects_the_requested_type(memory):
    profile_id = _seed(memory, 1, SECRET, memory_type="profile",
                       subject=SECRET_SUBJECT)
    pref_id = _seed(memory, 1, OTHER_CLAIM, memory_type="preference")
    count = memory.clear(1, memory_type="profile")
    assert count == 1
    assert _raw(memory, "SELECT claim, status FROM memories WHERE memory_id = ?",
                (profile_id,))[0] == (ERASED_PLACEHOLDER, "forgotten")
    # unrelated type: row, status and its version history are untouched
    assert _raw(memory, "SELECT claim, status FROM memories WHERE memory_id = ?",
                (pref_id,))[0] == (OTHER_CLAIM, "active")
    assert all(v[0] == OTHER_CLAIM for v in _raw(
        memory, "SELECT claim FROM memory_versions WHERE memory_id = ?", (pref_id,)))


def test_clear_all_scrubs_every_memory_of_the_owner_but_no_other_owner(memory):
    _seed(memory, 1, SECRET, memory_type="profile", subject=SECRET_SUBJECT)
    _seed(memory, 1, OTHER_CLAIM, memory_type="preference")
    foreign_id = _seed(memory, 2, "owner two keeps this", memory_type="preference")
    assert memory.clear(1) == 2
    for cell in _all_text_cells(memory):
        assert SECRET not in cell and OTHER_CLAIM not in cell
    assert _raw(memory, "SELECT claim, status FROM memories WHERE memory_id = ?",
                (foreign_id,))[0] == ("owner two keeps this", "active")


def test_clear_writes_one_content_free_audit_event(memory):
    _seed(memory, 1, SECRET, memory_type="profile", subject=SECRET_SUBJECT)
    memory.clear(1)
    rows = _raw(memory, "SELECT action, memory_id, reason FROM memory_events "
                        "WHERE action = 'cleared'")
    assert rows == [("cleared", None, "")]


# -------------------------------------------------------------- hard delete --- #
def test_hard_delete_removes_the_row_and_its_versions(seeded):
    memory, secret_id, kept_id = seeded
    assert memory.delete(1, secret_id) is True
    assert _raw(memory, "SELECT COUNT(*) FROM memories WHERE memory_id = ?",
                (secret_id,))[0][0] == 0
    assert _raw(memory, "SELECT COUNT(*) FROM memory_versions WHERE memory_id = ?",
                (secret_id,))[0][0] == 0
    # audit rows survive but no longer point at the deleted row
    assert _raw(memory, "SELECT COUNT(*) FROM memory_events WHERE memory_id = ?",
                (secret_id,))[0][0] == 0
    assert _raw(memory, "SELECT COUNT(*) FROM memory_access_log WHERE memory_id = ?",
                (secret_id,))[0][0] == 0


def test_hard_delete_leaves_no_secret_in_any_dependent_row(seeded):
    memory, secret_id, _ = seeded
    memory.delete(1, secret_id)
    for cell in _all_text_cells(memory):
        assert SECRET not in cell
        assert SECRET_SUBJECT not in cell
        assert SECRET_NOTE not in cell
        assert SECRET_META not in cell


def test_hard_delete_keeps_the_unrelated_memory_intact(seeded):
    memory, secret_id, kept_id = seeded
    memory.delete(1, secret_id)
    assert _raw(memory, "SELECT claim, status FROM memories WHERE memory_id = ?",
                (kept_id,))[0] == (OTHER_CLAIM, "active")
    assert _raw(memory, "SELECT COUNT(*) FROM memory_versions WHERE memory_id = ?",
                (kept_id,))[0][0] >= 1


def test_hard_delete_of_a_foreign_memory_changes_nothing(seeded):
    memory, secret_id, _ = seeded
    assert memory.delete(2, secret_id) is False
    assert _raw(memory, "SELECT claim FROM memories WHERE memory_id = ?",
                (secret_id,))[0][0] == SECRET


def test_delete_audit_event_is_written_without_a_dangling_memory_id(seeded):
    """Regression: the audit row used to be inserted AFTER the row was removed,
    pointing at a memory_id that no longer existed (a foreign-key violation on
    PostgreSQL). It is now written in the same transaction with memory_id NULL."""
    memory, secret_id, _ = seeded
    memory.delete(1, secret_id)
    rows = _raw(memory, "SELECT action, memory_id FROM memory_events "
                        "WHERE action = 'deleted'")
    assert rows == [("deleted", None)]


# ----------------------------------------------------------------- atomicity --- #
class _FailingCursor:
    def __init__(self, inner, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on

    def execute(self, sql, params=()):
        if self._fail_on in sql:
            raise RuntimeError("injected database failure")
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailingConnection:
    def __init__(self, inner, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on

    def cursor(self):
        return _FailingCursor(self._inner.cursor(), self._fail_on)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("fail_on", [
    "UPDATE memory_versions",       # after nothing is changed yet
    "UPDATE memories SET status",   # after history was already redacted
    "INSERT INTO memory_events",    # after the row was already scrubbed
])
def test_failure_midway_rolls_back_and_reports_no_success(seeded, monkeypatch, fail_on):
    memory, secret_id, _ = seeded
    repo = memory.store.memories
    real_connection = repo._connection

    monkeypatch.setattr(type(repo), "_connection",
                        lambda self: _FailingConnection(real_connection(), fail_on))
    with pytest.raises(EraseFailed):
        repo.erase(1, memory_id=secret_id, audit={"action": "forgotten",
                                                  "source": "user", "actor": 1,
                                                  "memory_id": secret_id})
    monkeypatch.setattr(type(repo), "_connection", lambda self: real_connection())

    # nothing was partially applied: the row, its history and its audit trail are intact
    assert _raw(memory, "SELECT claim, status, subject FROM memories WHERE memory_id = ?",
                (secret_id,))[0] == (SECRET, "active", SECRET_SUBJECT)
    assert all(v[0] == SECRET for v in _raw(
        memory, "SELECT claim FROM memory_versions WHERE memory_id = ?", (secret_id,)))
    assert _raw(memory, "SELECT COUNT(*) FROM memory_events WHERE action = 'forgotten'")[0][0] == 0


def test_service_forget_returns_false_when_the_erasure_fails(seeded, monkeypatch):
    memory, secret_id, _ = seeded
    repo = memory.store.memories
    real_connection = repo._connection
    monkeypatch.setattr(type(repo), "_connection",
                        lambda self: _FailingConnection(real_connection(),
                                                        "UPDATE memories SET status"))
    assert memory.forget(1, secret_id) is False
    monkeypatch.setattr(type(repo), "_connection", lambda self: real_connection())
    assert _raw(memory, "SELECT claim FROM memories WHERE memory_id = ?",
                (secret_id,))[0][0] == SECRET


def test_service_clear_raises_instead_of_reporting_a_partial_success(seeded, monkeypatch):
    memory, _, _ = seeded
    repo = memory.store.memories
    real_connection = repo._connection
    monkeypatch.setattr(type(repo), "_connection",
                        lambda self: _FailingConnection(real_connection(),
                                                        "INSERT INTO memory_events"))
    with pytest.raises(EraseFailed):
        memory.clear(1)
    monkeypatch.setattr(type(repo), "_connection", lambda self: real_connection())
    assert len(memory.list(1)) == 2  # nothing was forgotten
