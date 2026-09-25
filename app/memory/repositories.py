"""AUSTRO AI - Memory persistence layer.

Owner-scoped repositories over the shared SQLite manager. Every memory row is
bound to ``owner_user_id`` and every read filters by it, so no user can ever
see another user's memories. All SQL lives here; the service layer never talks
to the database directly. Mirrors the knowledge engine's repository style
(thread-local connections, falsy-on-error returns, dict-shaped rows).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.connection import DatabaseManager
from app.database.dialect import DB_ERROR
from app.memory.models import (
    MemoryEventRecord,
    MemoryItem,
    MemoryPrefs,
    build_hash_key,
)

logger = logging.getLogger(__name__)


class _MemoryBase:
    def __init__(self, manager: DatabaseManager):
        self._manager = manager

    def _connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _j(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False) if value else "{}"

    @classmethod
    def _unjson(cls, value: Any, default: Any = None) -> Any:
        if not value:
            return default if default is not None else {}
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}

    @classmethod
    def _memory_row(cls, row: Optional[sqlite3.Row]) -> Optional[MemoryItem]:
        if row is None:
            return None
        return MemoryItem(
            memory_id=row["memory_id"],
            owner_user_id=row["owner_user_id"],
            scope=row["scope"],
            memory_type=row["memory_type"],
            subject=row["subject"] or "",
            claim=row["claim"],
            confidence=row["confidence"],
            importance=row["importance"],
            provenance=row["provenance"],
            source_note=row["source_note"] or "",
            consent_state=row["consent_state"],
            decay_policy=row["decay_policy"] or "default",
            is_episodic=bool(row["is_episodic"]),
            event_date=row["event_date"],
            status=row["status"],
            version=row["version"],
            hash_key=row["hash_key"],
            metadata=cls._unjson(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            last_confirmed_at=row["last_confirmed_at"],
        )

    @classmethod
    def _memory_dict(cls, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        item = cls._memory_row(row)
        return item.as_dict() if item else None


class MemoriesRepository(_MemoryBase):
    """CRUD over the authoritative typed ``memories`` table."""

    _INSERT_COLUMNS = (
        "owner_user_id, scope, memory_type, subject, claim, confidence, "
        "importance, provenance, source_note, consent_state, decay_policy, "
        "is_episodic, event_date, status, version, hash_key, metadata_json, "
        "created_at, updated_at"
    )

    def create(self, item: MemoryItem) -> Optional[int]:
        """Insert a memory row; returns the new memory_id."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                now = self._now()
                cursor.execute(
                    f"INSERT INTO memories ({self._INSERT_COLUMNS}) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.owner_user_id,
                        item.scope,
                        item.memory_type,
                        item.subject,
                        item.claim,
                        item.confidence,
                        item.importance,
                        item.provenance,
                        item.source_note or "",
                        item.consent_state,
                        item.decay_policy or "default",
                        int(item.is_episodic),
                        item.event_date,
                        item.status or "active",
                        item.version or 1,
                        item.hash_key,
                        self._j(item.metadata),
                        now,
                        now,
                    ),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in memory create: {e}")
            return None

    def get(self, owner_user_id: int, memory_id: int) -> Optional[MemoryItem]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memories "
                    "WHERE memory_id = ? AND owner_user_id = ?",
                    (memory_id, owner_user_id),
                )
                return self._memory_row(cursor.fetchone())
        except DB_ERROR as e:
            logger.error(f"Database error in memory get: {e}")
            return None

    def by_hash(self, owner_user_id: int, scope: str, memory_type: str,
                subject: str, claim: str) -> Optional[MemoryItem]:
        """Lookup for exact dedup; owner-scoped."""
        hash_key = build_hash_key(owner_user_id, scope, memory_type, subject, claim)
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memories "
                    "WHERE owner_user_id = ? AND hash_key = ?",
                    (owner_user_id, hash_key),
                )
                return self._memory_row(cursor.fetchone())
        except DB_ERROR as e:
            logger.error(f"Database error in memory by_hash: {e}")
            return None

    def find_slot(self, owner_user_id: int, scope: str, memory_type: str,
                  subject: str) -> Optional[MemoryItem]:
        """Find a memory occupying the same typed 'slot' (conflict target).

        Slots are compared by normalized subject in Python so that casing and
        whitespace differences still match the stored value exactly.
        """
        from app.memory.models import subject_key

        target = subject_key(scope, memory_type, subject)
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memories WHERE owner_user_id = ? AND scope = ? "
                    "AND memory_type = ? AND status = 'active' "
                    "ORDER BY updated_at DESC",
                    (owner_user_id, scope, memory_type),
                )
                latest: Optional[MemoryItem] = None
                for row in cursor.fetchall():
                    item = self._memory_row(row)
                    if item and subject_key(scope, memory_type, item.subject) == target:
                        latest = item
                return latest
        except DB_ERROR as e:
            logger.error(f"Database error in memory find_slot: {e}")
            return None

    def list(self, owner_user_id: int, memory_type: Optional[str] = None,
             scope: str = "USER", status: str = "active",
             limit: int = 100) -> List[MemoryItem]:
        """List memories newest-first, optionally filtered by type."""
        rows: List[MemoryItem] = []
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if memory_type:
                    cursor.execute(
                        "SELECT * FROM memories WHERE owner_user_id = ? "
                        "AND scope = ? AND memory_type = ? AND status = ? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (owner_user_id, scope, memory_type, status, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM memories WHERE owner_user_id = ? "
                        "AND scope = ? AND status = ? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (owner_user_id, scope, status, limit),
                    )
                for row in cursor.fetchall():
                    item = self._memory_row(row)
                    if item:
                        rows.append(item)
        except DB_ERROR as e:
            logger.error(f"Database error in memory list: {e}")
        return rows

    def search(self, owner_user_id: int, text: str, limit: int = 25,
               memory_type: Optional[str] = None) -> List[MemoryItem]:
        """Lexical search over subject + claim (deterministic, cheap)."""
        rows: List[MemoryItem] = []
        pattern = f"%{text}%"
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if memory_type:
                    cursor.execute(
                        "SELECT * FROM memories WHERE owner_user_id = ? "
                        "AND scope = 'USER' AND status = 'active' "
                        "AND memory_type = ? AND (subject LIKE ? OR claim LIKE ?) "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (owner_user_id, memory_type, pattern, pattern, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM memories WHERE owner_user_id = ? "
                        "AND scope = 'USER' AND status = 'active' "
                        "AND (subject LIKE ? OR claim LIKE ?) "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (owner_user_id, pattern, pattern, limit),
                    )
                for row in cursor.fetchall():
                    item = self._memory_row(row)
                    if item:
                        rows.append(item)
        except DB_ERROR as e:
            logger.error(f"Database error in memory search: {e}")
        return rows

    def counts(self, owner_user_id: int) -> Dict[str, int]:
        """Per-type + total counts for the memory screen (cheap aggregates)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT memory_type, COUNT(*) AS n FROM memories "
                    "WHERE owner_user_id = ? AND scope = 'USER' "
                    "AND status = 'active' GROUP BY memory_type",
                    (owner_user_id,),
                )
                counts = {row["memory_type"]: row["n"] for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT COUNT(*) AS total FROM memories "
                    "WHERE owner_user_id = ? AND scope = 'USER' "
                    "AND status = 'active'",
                    (owner_user_id,),
                )
                total = cursor.fetchone()["total"]
                counts["total"] = total
                return counts
        except DB_ERROR as e:
            logger.error(f"Database error in memory counts: {e}")
            return {"total": 0}

    def update(self, owner_user_id: int, memory_id: int, *, claim: str,
               confidence: str, importance: int, source_note: str = "",
               consent_state: str = "automatic") -> bool:
        """Overwrite a memory's value (used by conflict resolution / user edit)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE memories SET claim = ?, confidence = ?, "
                    "importance = ?, source_note = ?, consent_state = ?, "
                    "updated_at = ? WHERE memory_id = ? AND owner_user_id = ?",
                    (claim, confidence, importance, source_note,
                     consent_state, self._now(), memory_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory update: {e}")
            return False

    def update_hash_key(self, owner_user_id: int, memory_id: int,
                        hash_key: str) -> bool:
        """Recompute the dedup hash after a value conflict update."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE memories SET hash_key = ? "
                    "WHERE memory_id = ? AND owner_user_id = ?",
                    (hash_key, memory_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory update_hash_key: {e}")
            return False

    def touch(self, owner_user_id: int, memory_id: int) -> None:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE memories SET last_used_at = ? "
                    "WHERE memory_id = ? AND owner_user_id = ?",
                    (self._now(), memory_id, owner_user_id),
                )
                self._connection().commit()
        except DB_ERROR as e:  # pragma: no cover - defensive
            logger.error(f"Database error in memory touch: {e}")

    def confirm(self, owner_user_id: int, memory_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                now = self._now()
                cursor.execute(
                    "UPDATE memories SET last_confirmed_at = ?, "
                    "consent_state = 'explicit' WHERE memory_id = ? "
                    "AND owner_user_id = ?",
                    (now, memory_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory confirm: {e}")
            return False

    def set_status(self, owner_user_id: int, memory_id: int, status: str) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE memories SET status = ? WHERE memory_id = ? "
                    "AND owner_user_id = ?",
                    (status, memory_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory set_status: {e}")
            return False

    def forget(self, owner_user_id: int, memory_id: int) -> bool:
        return self.set_status(owner_user_id, memory_id, "forgotten")

    def resurrect(self, owner_user_id: int, memory_id: int,
                  consent_state: str = "automatic") -> bool:
        """Reactivate a soft-deleted (forgotten) memory row for the same owner.

        Used when the exact same statement is re-learned after a clear/forget,
        since the reused hash key must not violate the UNIQUE constraint.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE memories SET status = 'active', consent_state = ?, "
                    "updated_at = ?, last_used_at = ? "
                    "WHERE memory_id = ? AND owner_user_id = ?",
                    (consent_state, self._now(), self._now(), memory_id,
                     owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory resurrect: {e}")
            return False

    def delete(self, owner_user_id: int, memory_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "DELETE FROM memories WHERE memory_id = ? AND owner_user_id = ?",
                    (memory_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in memory delete: {e}")
            return False

    def clear(self, owner_user_id: int, memory_type: Optional[str] = None) -> int:
        """Mark all (optionally typed) memories forgotten; returns count."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if memory_type:
                    cursor.execute(
                        "UPDATE memories SET status = 'forgotten' WHERE "
                        "owner_user_id = ? AND memory_type = ?",
                        (owner_user_id, memory_type),
                    )
                else:
                    cursor.execute(
                        "UPDATE memories SET status = 'forgotten' WHERE "
                        "owner_user_id = ?",
                        (owner_user_id,),
                    )
                self._connection().commit()
                return cursor.rowcount
        except DB_ERROR as e:
            logger.error(f"Database error in memory clear: {e}")
            return 0

    def export(self, owner_user_id: int) -> List[Dict[str, Any]]:
        """Full snapshot of active memories for JSON export."""
        items = self.list(owner_user_id, status="active", limit=10000)
        payload = []
        for item in items:
            data = item.as_dict()
            data.pop("hash_key", None)
            payload.append(data)
        return payload

    def recent_used_ids(self, owner_user_id: int, limit: int = 50
                        ) -> List[MemoryItem]:
        """Memories ranked by recency of use (fallback ranking candidate)."""
        rows: List[MemoryItem] = []
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memories WHERE owner_user_id = ? "
                    "AND scope = 'USER' AND status = 'active' "
                    "ORDER BY last_used_at IS NULL, last_used_at DESC "
                    "LIMIT ?",
                    (owner_user_id, limit),
                )
                for row in cursor.fetchall():
                    item = self._memory_row(row)
                    if item:
                        rows.append(item)
        except DB_ERROR as e:
            logger.error(f"Database error in memory recent_used: {e}")
        return rows


class MemoryVersionsRepository(_MemoryBase):
    """Immutable history of memory value changes (conflict resolution trail)."""

    def add(self, *, memory_id: int, owner_user_id: int, version: int,
            claim: str, confidence: str, importance: int, reason: str,
            actor: Optional[int] = None) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO memory_versions "
                    "(memory_id, owner_user_id, version, claim, confidence, "
                    "importance, reason, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, owner_user_id, version, claim, confidence,
                     importance, reason, actor),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in memory version add: {e}")
            return None

    def list(self, owner_user_id: int, memory_id: int) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memory_versions WHERE memory_id = ? "
                    "AND owner_user_id = ? ORDER BY version DESC LIMIT 20",
                    (memory_id, owner_user_id),
                )
                for row in cursor.fetchall():
                    rows.append(dict(row))
        except DB_ERROR as e:
            logger.error(f"Database error in memory versions list: {e}")
        return rows


class MemoryEventsRepository(_MemoryBase):
    """Audit trail of every memory lifecycle action."""

    def log(self, *, owner_user_id: int, memory_id: Optional[int], action: str,
            source: str = "", reason: str = "", actor: Optional[int] = None
            ) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO memory_events "
                    "(owner_user_id, memory_id, action, source, reason, actor) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (owner_user_id, memory_id, action, source, reason, actor),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in memory event log: {e}")
            return None

    def list(self, owner_user_id: int, limit: int = 20,
             memory_id: Optional[int] = None) -> List[MemoryEventRecord]:
        rows: List[MemoryEventRecord] = []
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if memory_id:
                    cursor.execute(
                        "SELECT * FROM memory_events WHERE owner_user_id = ? "
                        "AND memory_id = ? ORDER BY created_at DESC LIMIT ?",
                        (owner_user_id, memory_id, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM memory_events WHERE owner_user_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (owner_user_id, limit),
                    )
                for row in cursor.fetchall():
                    rows.append(MemoryEventRecord(
                        event_id=row["event_id"],
                        owner_user_id=row["owner_user_id"],
                        memory_id=row["memory_id"],
                        action=row["action"],
                        source=row["source"] or "",
                        reason=row["reason"] or "",
                        actor=row["actor"],
                        created_at=row["created_at"],
                    ))
        except DB_ERROR as e:
            logger.error(f"Database error in memory events list: {e}")
        return rows


class MemoryAccessRepository(_MemoryBase):
    """Audit trail of memory reads (context packs, search, export)."""

    def log(self, *, owner_user_id: int, memory_id: Optional[int] = None,
            purpose: str = "context_pack") -> None:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO memory_access_log "
                    "(owner_user_id, memory_id, purpose) VALUES (?, ?, ?)",
                    (owner_user_id, memory_id, purpose),
                )
                self._connection().commit()
        except DB_ERROR as e:  # pragma: no cover - defensive
            logger.error(f"Database error in memory access log: {e}")

    def count(self, owner_user_id: int, purpose: Optional[str] = None) -> int:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if purpose:
                    cursor.execute(
                        "SELECT COUNT(*) AS n FROM memory_access_log "
                        "WHERE owner_user_id = ? AND purpose = ?",
                        (owner_user_id, purpose),
                    )
                else:
                    cursor.execute(
                        "SELECT COUNT(*) AS n FROM memory_access_log "
                        "WHERE owner_user_id = ?",
                        (owner_user_id,),
                    )
                return cursor.fetchone()["n"]
        except DB_ERROR as e:
            logger.error(f"Database error in memory access count: {e}")
            return 0


class MemoryPrefsRepository(_MemoryBase):
    """Per-user memory settings."""

    def get(self, owner_user_id: int) -> Optional[MemoryPrefs]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM memory_prefs WHERE owner_user_id = ?",
                    (owner_user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return MemoryPrefs(
                    owner_user_id=row["owner_user_id"],
                    auto_memory_enabled=bool(row["auto_memory_enabled"]),
                    consent_types=self._unjson(row["consent_types"]),
                    updated_at=row["updated_at"],
                )
        except DB_ERROR as e:
            logger.error(f"Database error in memory prefs get: {e}")
            return None

    def ensure(self, owner_user_id: int) -> MemoryPrefs:
        prefs = self.get(owner_user_id)
        if prefs is not None:
            return prefs
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO memory_prefs (owner_user_id, auto_memory_enabled, "
                    "consent_types) VALUES (?, 1, '{}')",
                    (owner_user_id,),
                )
                self._connection().commit()
        except DB_ERROR as e:
            logger.error(f"Database error in memory prefs ensure: {e}")
        return MemoryPrefs(owner_user_id=owner_user_id, auto_memory_enabled=True)

    def set_auto_enabled(self, owner_user_id: int, enabled: bool) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO memory_prefs (owner_user_id, auto_memory_enabled, "
                    "consent_types) VALUES (?, ?, '{}') "
                    "ON CONFLICT(owner_user_id) DO UPDATE SET "
                    "auto_memory_enabled = excluded.auto_memory_enabled, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (owner_user_id, int(enabled)),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in memory prefs set_auto: {e}")
            return False

    def set_consent(self, owner_user_id: int, memory_type: str, state: str) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                prefs = self.ensure(owner_user_id)
                consent = dict(prefs.consent_types)
                consent[memory_type] = state
                cursor.execute(
                    "INSERT INTO memory_prefs (owner_user_id, auto_memory_enabled, "
                    "consent_types) VALUES (?, ?, ?) "
                    "ON CONFLICT(owner_user_id) DO UPDATE SET "
                    "consent_types = excluded.consent_types, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (
                        owner_user_id,
                        int(prefs.auto_memory_enabled),
                        self._j(consent),
                    ),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in memory prefs consent: {e}")
            return False


class MemoryStore:
    """Aggregate root exposing every owner-scoped memory repository."""

    def __init__(self, manager: DatabaseManager):
        self.memories = MemoriesRepository(manager)
        self.versions = MemoryVersionsRepository(manager)
        self.events = MemoryEventsRepository(manager)
        self.access = MemoryAccessRepository(manager)
        self.prefs = MemoryPrefsRepository(manager)