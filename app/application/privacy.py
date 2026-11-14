"""Account-level privacy operations: export everything, erase everything.

WHY THIS MODULE EXISTS
=====================
`PRIVACY_RETENTION.md` documented an honest gap: the bot had no `/export` and no
`/delete`, so a user could not obtain or erase the data the product stores about
them, and most tables were retained indefinitely. The pieces to fix that existed
in isolation - ``LearningEngine.export_learning``, ``MemoryService.export``,
``KnowledgeService.delete_source`` - but nothing composed them, nothing erased
the core tables, and nothing removed files from disk.

The rules this service enforces:

1. **Deletion is all-or-nothing.** The database rows are erased in a single
   transaction (``PrivacyRepository.delete_all``). Only after the commit do we
   remove the uploaded files, because a file is recoverable from its row but a
   row is not recoverable from its file: if the transaction fails, the account
   is untouched and the user is told so.
2. **Deletion is verified, not assumed.** ``delete_account`` re-reads the counts
   afterwards and reports ``verified`` only when no owned row remains. A caller
   must never claim "your data is gone" without that read; if the verification
   read itself fails, the result says so instead of treating the error as an
   empty account.
3. **Export is JSON-safe.** Rows can contain ``datetime``, ``Decimal`` and
   ``bytes``; the snapshot is normalised so it can be written to a file and sent
   to the user without a serializer crash.
4. **Export is scoped.** Every query is filtered by the owner id; an export for
   one user can never contain another user's rows.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.database.repositories import Database

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Normalise a database value into something ``json.dumps`` accepts."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Never dump raw bytes into a user-facing export: it is unreadable and
        # may hold file content. Report its size instead.
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _json_safe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{str(k): _json_safe(v) for k, v in row.items()} for row in rows]


@dataclass
class ExportResult:
    """Outcome of an account export."""

    payload: Dict[str, Any]
    total_rows: int

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True)

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")


@dataclass
class DeletionResult:
    """Outcome of an account deletion.

    ``verified`` is the only field a caller may present as "your data is gone";
    it is set from a re-read of the database after the commit, never from the
    fact that the delete statements returned.
    """

    deleted: Dict[str, int]
    files_removed: int
    files_failed: int
    verified: bool
    #: ``None`` means the post-delete verification read failed, so the
    #: outcome is unknown; ``{}`` means it succeeded and found nothing.
    residual: Optional[Dict[str, int]]

    @property
    def total_rows(self) -> int:
        return sum(self.deleted.values())


class PrivacyService:
    """Compose export/erasure across the core, learning, memory and knowledge stores."""

    def __init__(self, db: Database, knowledge_storage=None, knowledge=None,
                 learning_engine=None, memory=None):
        self._db = db
        # All collaborators are optional so the service is usable (and testable)
        # with only the database, e.g. in a migration script.
        self._storage = knowledge_storage
        self._knowledge = knowledge
        self._learning_engine = learning_engine
        self._memory = memory

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export(self, user_id: int) -> Optional[ExportResult]:
        """Snapshot everything this user owns.

        Returns ``None`` only when the database read itself failed, so the
        caller can tell "empty account" apart from "we could not read it".
        """
        rows = self._db.privacy.export_rows(user_id)
        if not rows:
            logger.error("Privacy export failed for user %s", user_id)
            return None

        payload: Dict[str, Any] = {
            "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "user_id": user_id,
            "format_version": 1,
            "tables": {name: _json_safe_rows(items) for name, items in rows.items()},
        }

        # Rich exports from the domain facades, when they are wired in. Failures
        # are reported inside the payload rather than aborting the whole export:
        # the core snapshot above is already complete and useful.
        if self._learning_engine is not None:
            try:
                payload["learning"] = _json_safe(
                    self._learning_engine.export_learning(user_id)
                )
            except Exception as exc:  # noqa: BLE001 - one section must not lose the rest
                logger.warning("Learning export failed for %s: %s", user_id, type(exc).__name__)
                payload["learning"] = {"error": type(exc).__name__}

        if self._memory is not None:
            try:
                payload["memories"] = _json_safe(
                    self._memory.export(user_id) or {}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory export failed for %s: %s", user_id, type(exc).__name__)
                payload["memories"] = {"error": type(exc).__name__}

        total = sum(len(items) for items in rows.values())
        return ExportResult(payload=payload, total_rows=total)

    # ------------------------------------------------------------------
    # Erasure
    # ------------------------------------------------------------------
    def delete_account(self, user_id: int) -> Optional[DeletionResult]:
        """Erase every row and stored file belonging to this user.

        Returns ``None`` when the database transaction failed, in which case
        **nothing** was deleted. Uploaded files are removed only after the
        commit, and every file failure is counted instead of raised so a stale
        file never hides a successful database erasure.
        """
        storage_keys: Optional[List[str]] = []
        if self._storage is not None:
            storage_keys = self._db.privacy.storage_keys(user_id)
            if storage_keys is None:
                # The file keys live in rows that the transaction is about to
                # delete. Without them the files can never be located again, so
                # abort before deleting anything rather than leak orphan files.
                logger.error(
                    "Could not read storage keys for %s; erasure aborted", user_id
                )
                return None

        deleted = self._db.privacy.delete_all(user_id)
        if deleted is None:
            return None

        files_removed = 0
        files_failed = 0
        if self._storage is not None:
            for key in storage_keys:
                try:
                    if self._storage.delete(key):
                        files_removed += 1
                    else:
                        files_failed += 1
                except Exception:  # noqa: BLE001 - a file must not undo the commit
                    logger.warning("Could not remove stored file %s", key)
                    files_failed += 1

        # Verify from the database, never from the delete return codes.
        residual_counts = self._db.privacy.counts(user_id)
        if residual_counts is None:
            # The erasure committed, but the check that proves it did not run.
            # Report "unverified", never "empty": an unreadable database and a
            # clean deletion are different facts.
            residual = None
            verified = False
            logger.error(
                "Account deletion for %s could not be verified: counts read failed",
                user_id,
            )
        else:
            residual = {
                name: count for name, count in residual_counts.items() if count
            }
            verified = not residual
            if not verified:
                # A row survived: either a table is missing from the erasure
                # lists (a bug worth logging loudly) or a concurrent writer
                # re-created data. Never report a clean deletion in that case.
                logger.error(
                    "Account deletion for %s left residual rows: %s",
                    user_id,
                    residual,
                )

        return DeletionResult(
            deleted=deleted,
            files_removed=files_removed,
            files_failed=files_failed,
            verified=verified,
            residual=residual,
        )

    def summary(self, user_id: int) -> Optional[Dict[str, int]]:
        """Current per-table row counts, or ``None`` when they cannot be read."""
        return self._db.privacy.counts(user_id)

    def has_any_data(self, user_id: int) -> bool:
        summary = self.summary(user_id)
        return bool(summary) and any(summary.values())
