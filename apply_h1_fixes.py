#!/usr/bin/env python3
"""Apply all H1 fixes to the codebase."""

import sys

# ===== Fix app/database/migrations.py =====
with open('app/database/migrations.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_migrations = """MIGRATIONS: List[Migration] = [KNOWLEDGE_V1, MEMORY_V1, LEARNING_V1, CORE_V1]"""
new_migrations = """# Knowledge engine schema v2: add claim_token and claim_expires_at for ingestion concurrency control.
KNOWLEDGE_V2 = Migration(
    schema_name="knowledge",
    version=2,
    statements=[
        """
        ALTER TABLE knowledge_sources ADD COLUMN claim_token TEXT
        """,
        """
        ALTER TABLE knowledge_sources ADD COLUMN claim_expires_at TIMESTAMP
        """,
    ],
)

MIGRATIONS: List[Migration] = [KNOWLEDGE_V1, KNOWLEDGE_V2, MEMORY_V1, LEARNING_V1, CORE_V1]"""

if old_migrations in content:
    content = content.replace(old_migrations, new_migrations)
    print("1. Added KNOWLEDGE_V2 migration")
else:
    print("ERROR: Could not find old migrations pattern")
    sys.exit(1)

with open('app/database/migrations.py', 'w', encoding='utf-8') as f:
    f.write(content)

# ===== Fix app/knowledge/repositories.py =====
with open('app/knowledge/repositories.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add renew_claim method to KnowledgeSourceRepository (after clear_claim, before set_title)
old_repo_1 = '''    def clear_claim(self, source_id: int, claim_token: str) -> int:
        """Release a claim only when the supplied token matches the current owner.

        Returns the number of rows affected (0 or 1). A return value of 1 means
        the claim was released; 0 means the token did not match the current holder.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                sql = (
                    "UPDATE knowledge_sources "
                    "SET claim_token = NULL, claim_expires_at = NULL "
                    "WHERE source_id = ? AND claim_token = ?"
                )
                cursor.execute(sql, (source_id, claim_token))
                rows_affected = cursor.rowcount
                self._connection().commit()
                return rows_affected
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge clear_claim: {e}")
            return 0

    def set_title(self, owner_user_id: int, source_id: int, title: str,'''

new_repo_1 = '''    def clear_claim(self, source_id: int, claim_token: str) -> int:
        """Release a claim only when the supplied token matches the current owner.

        Returns the number of rows affected (0 or 1). A return value of 1 means
        the claim was released; 0 means the token did not match the current holder.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                sql = (
                    "UPDATE knowledge_sources "
                    "SET claim_token = NULL, claim_expires_at = NULL "
                    "WHERE source_id = ? AND claim_token = ?"
                )
                cursor.execute(sql, (source_id, claim_token))
                rows_affected = cursor.rowcount
                self._connection().commit()
                return rows_affected
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge clear_claim: {e}")
            return 0

    def complete_with_claim(self, owner_user_id: int, source_id: int,
                             claim_token: str, status: str, state: str) -> bool:
        """Atomically complete ingestion if the claim token matches.

        Verifies that the supplied claim_token matches the current holder,
        then atomically sets the final status and ingestion_state.
        Returns True if the claim was verified and the state was updated,
        False if the token did not match (another worker owns the source).
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                sql = (
                    "UPDATE knowledge_sources SET status = ?, ingestion_state = ?, "
                    "claim_token = NULL, claim_expires_at = NULL, updated_at = ? "
                    "WHERE source_id = ? AND owner_user_id = ? AND claim_token = ?"
                )
                cursor.execute(sql, (status, state, datetime.now().isoformat(),
                                     source_id, owner_user_id, claim_token))
                rows_affected = cursor.rowcount
                self._connection().commit()
                return rows_affected > 0
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge complete_with_claim: {e}")
            return False

    def renew_claim(self, *, source_id: int, claim_token: str,
                      claim_expires_at: datetime) -> int:
        """Attempt to renew an existing claim.

        Extends the claim expiration only when the current claim_token matches
        and the lease is still valid (datetime(claim_expires_at) > CURRENT_TIMESTAMP).
        Returns the number of rows affected (0 or 1). A return value of 1 means
        the claim was successfully renewed; 0 means the claim was not found,
        already expired, or owned by another worker.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                sql = (
                    "UPDATE knowledge_sources "
                    "SET claim_expires_at = ?, updated_at = ? "
                    "WHERE source_id = ? AND claim_token = ? AND datetime(claim_expires_at) > CURRENT_TIMESTAMP"
                )
                cursor.execute(sql, (claim_expires_at, datetime.now().isoformat(),
                                     source_id, claim_token))
                rows_affected = cursor.rowcount
                self._connection().commit()
                return rows_affected
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge renew_claim: {e}")
            return 0

    def set_title(self, owner_user_id: int, source_id: int, title: str,'''

if old_repo_1 in content:
    content = content.replace(old_repo_1, new_repo_1)
    print("2. Added complete_with_claim and renew_claim to KnowledgeSourceRepository")
else:
    print("ERROR: Could not find clear_claim/set_title pattern")
    sys.exit(1)

# 2. Add cleanup_for_source to KnowledgeDocumentRepository (after list_for_source)
old_repo_2 = '''    def list_for_source(self, source_id: int) -> List[Dict[str, Any]]:
        """Documents referencing a source, newest version first."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_documents WHERE source_id = ? "
                    "ORDER BY version DESC",
                    (source_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["metadata"] = self._unjson(row["metadata_json"])
                    data.pop("metadata_json", None)
                    data["toc"] = self._unjson(row["toc_json"])
                    data.pop("toc_json", None)
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge documents list_for_source: {e}")
            return []


class KnowledgeSectionRepository(_KnowledgeBase):'''

new_repo_2 = '''    def list_for_source(self, source_id: int) -> List[Dict[str, Any]]:
        """Documents referencing a source, newest version first."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_documents WHERE source_id = ? "
                    "ORDER BY version DESC",
                    (source_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["metadata"] = self._unjson(row["metadata_json"])
                    data.pop("metadata_json", None)
                    data["toc"] = self._unjson(row["toc_json"])
                    data.pop("toc_json", None)
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge documents list_for_source: {e}")
            return []

    def cleanup_for_source(self, owner_user_id: int, source_id: int, claim_token: Optional[str] = None) -> bool:
        """Remove all document/section/chunk/embedding data for a source.

        Only removes data if the claim token matches the current claim holder
        (or if no claim is currently held). This prevents a stale worker from
        deleting data belonging to an active worker.
        """
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                # First, verify claim ownership if token provided
                if claim_token is not None:
                    cursor.execute(
                        "SELECT 1 FROM knowledge_sources WHERE source_id = ? AND owner_user_id = ? AND claim_token = ? AND datetime(claim_expires_at) > CURRENT_TIMESTAMP",
                        (source_id, owner_user_id, claim_token),
                    )
                    if not cursor.fetchone():
                        logger.warning(f"cleanup_for_source rejected: claim token mismatch or expired for source {source_id}")
                        return False

                # Delete embeddings first (FK constraint)
                cursor.execute(
                    "DELETE FROM knowledge_embeddings WHERE source_id = ?",
                    (source_id,),
                )
                # Delete chunks
                cursor.execute(
                    "DELETE FROM knowledge_chunks WHERE source_id = ?",
                    (source_id,),
                )
                # Delete sections
                cursor.execute(
                    "DELETE FROM knowledge_sections WHERE source_id = ?",
                    (source_id,),
                )
                # Delete documents
                cursor.execute(
                    "DELETE FROM knowledge_documents WHERE source_id = ? AND owner_user_id = ?",
                    (source_id, owner_user_id),
                )
                # Delete storage files
                cursor.execute(
                    "DELETE FROM knowledge_storage_files WHERE source_id = ?",
                    (source_id,),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            self._rollback()
            logger.error(f"Database error in knowledge cleanup_for_source: {e}")
            return False


class KnowledgeSectionRepository(_KnowledgeBase):'''

if old_repo_2 in content:
    content = content.replace(old_repo_2, new_repo_2)
    print("3. Added cleanup_for_source to KnowledgeDocumentRepository")
else:
    print("ERROR: Could not find list_for_source pattern")
    sys.exit(1)

# 3. Add cleanup_for_source and renew_claim to KnowledgeStore facade
old_repo_3 = '''    def cleanup_for_source(self, owner_user_id: int, source_id: int,
                           claim_token: Optional[str] = None) -> bool:
        """Remove all document/section/chunk/embedding data for a source.

        Delegates to the documents repository. The claim_token ensures
        ownership validation to prevent stale workers from deleting data.
        """
        return self.documents.cleanup_for_source(
            owner_user_id, source_id, claim_token=claim_token
        )


__all__ = ["KnowledgeStore"]'''

new_repo_3 = '''    def cleanup_for_source(self, owner_user_id: int, source_id: int,
                           claim_token: Optional[str] = None) -> bool:
        """Remove all document/section/chunk/embedding data for a source.

        Delegates to the documents repository. The claim_token ensures
        ownership validation to prevent stale workers from deleting data.
        """
        return self.documents.cleanup_for_source(
            owner_user_id, source_id, claim_token=claim_token
        )

    def renew_claim(self, *, source_id: int, claim_token: str,
                    claim_expires_at: datetime) -> int:
        """Attempt to renew an existing claim.

        Delegates to the sources repository. Returns the number of rows affected (0 or 1).
        """
        return self.sources.renew_claim(
            source_id=source_id, claim_token=claim_token,
            claim_expires_at=claim_expires_at
        )


__all__ = ["KnowledgeStore"]'''

if old_repo_3 in content:
    content = content.replace(old_repo_3, new_repo_3)
    print("4. Added cleanup_for_source and renew_claim to KnowledgeStore")
else:
    print("ERROR: Could not find KnowledgeStore cleanup pattern")
    sys.exit(1)

with open('app/knowledge/repositories.py', 'w', encoding='utf-8') as f:
    f.write(content)

# ===== Fix app/knowledge/ingestion.py =====
with open('app/knowledge/ingestion.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add imports
old_imports = """from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional"""

new_imports = """from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional"""

if old_imports in content:
    content = content.replace(old_imports, new_imports)
    print("5. Added imports to ingestion.py")
else:
    print("ERROR: Could not find old imports pattern in ingestion.py")
    sys.exit(1)

# 2. Add claim token handling to IngestionPipeline.__init__
old_init = """class IngestionPipeline:
    def __init__(self, store: KnowledgeStore, storage: StorageService,
                 extractor: Optional[TextExtractor] = None,
                 cleaner: Optional[TextCleaner] = None,
                 chunker: Optional[SemanticChunker] = None,
                 embeddings: Optional[EmbeddingService] = None,
                 settings_: Optional[Settings] = None):
        self._store = store
        self._storage = storage
        self._extractor = extractor or TextExtractor()
        self._cleaner = cleaner or TextCleaner()
        self._settings = settings_ or __import__(
            "app.config.settings", fromlist=["settings"]
        ).settings
        self._chunker = chunker or SemanticChunker(
            chunk_size=self._settings.knowledge_chunk_size,
            overlap=self._settings.knowledge_chunk_overlap,
            cleaner=self._cleaner,
        )
        self._embeddings = embeddings or EmbeddingService(self._settings)"""

new_init = """class IngestionPipeline:
    def __init__(self, store: KnowledgeStore, storage: StorageService,
                 extractor: Optional[TextExtractor] = None,
                 cleaner: Optional[TextCleaner] = None,
                 chunker: Optional[SemanticChunker] = None,
                 embeddings: Optional[EmbeddingService] = None,
                 settings_: Optional[Settings] = None):
        self._store = store
        self._storage = storage
        self._extractor = extractor or TextExtractor()
        self._cleaner = cleaner or TextCleaner()
        self._settings = settings_ or __import__(
            "app.config.settings", fromlist=["settings"]
        ).settings
        self._chunker = chunker or SemanticChunker(
            chunk_size=self._settings.knowledge_chunk_size,
            overlap=self._settings.knowledge_chunk_overlap,
            cleaner=self._cleaner,
        )
        self._embeddings = embeddings or EmbeddingService(self._settings)
        # Claim token for this ingestion run (set during claim acquisition)
        self._claim_token: Optional[str] = None
        # Claim TTL in minutes
        self._claim_ttl_minutes = 30
        # Renewal interval (half of TTL to ensure we renew before expiry)
        self._claim_renewal_interval_seconds = self._claim_ttl_minutes * 30
        # Background renewal task
        self._renewal_task: Optional[asyncio.Task] = None
        # Event to signal renewal task to stop
        self._renewal_stop_event: Optional[asyncio.Event] = None

    def _generate_claim_token(self) -> str:
        """Generate a unique claim token for this ingestion run."""
        return f"ingest-{uuid.uuid4().hex[:12]}"

    def _claim_expires_at(self) -> datetime:
        """Calculate claim expiration time."""
        return datetime.now(timezone.utc) + timedelta(minutes=self._claim_ttl_minutes)

    async def _start_claim_renewal(self, source_id: int) -> None:
        """Start background task to periodically renew the claim."""
        self._renewal_stop_event = asyncio.Event()
        self._renewal_task = asyncio.create_task(self._claim_renewal_loop(source_id))

    async def _claim_renewal_loop(self, source_id: int) -> None:
        """Background task to periodically renew the claim."""
        try:
            while not self._renewal_stop_event.is_set():
                # Wait for the renewal interval or until stop is signaled
                try:
                    await asyncio.wait_for(
                        self._renewal_stop_event.wait(),
                        timeout=self._claim_renewal_interval_seconds
                    )
                    # If we get here, stop was signaled
                    break
                except asyncio.TimeoutError:
                    # Timeout expired, time to renew
                    pass

                if self._renewal_stop_event.is_set():
                    break

                # Attempt to renew the claim
                claim_expires_at = self._claim_expires_at()
                rows_affected = await asyncio.to_thread(
                    self._store.renew_claim,
                    source_id=source_id,
                    claim_token=self._claim_token,
                    claim_expires_at=claim_expires_at,
                )
                if rows_affected == 0:
                    # Renewal failed - claim expired or taken by another worker
                    logger.warning(f"Claim renewal failed for source {source_id}, stopping renewal loop")
                    self._renewal_stop_event.set()
                    break

                logger.debug(f"Claim renewed for source {source_id} until {claim_expires_at.isoformat()}")

        except asyncio.CancelledError:
            # Task was cancelled, clean exit
            pass
        except Exception as e:
            logger.error(f"Error in claim renewal loop for source {source_id}: {e}")
            # On any error, stop the renewal loop
            self._renewal_stop_event.set()

    async def _stop_claim_renewal(self) -> None:
        """Stop the background claim renewal task."""
        if self._renewal_stop_event:
            self._renewal_stop_event.set()
        if self._renewal_task and not self._renewal_task.done():
            try:
                await asyncio.wait_for(self._renewal_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._renewal_task.cancel()
                try:
                    await self._renewal_task
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass"""

if old_init in content:
    content = content.replace(old_init, new_init)
    print("6. Added claim token handling to IngestionPipeline.__init__")
else:
    print("ERROR: Could not find __init__ pattern")
    sys.exit(1)

# 3. Replace process_source method
old_process = """async def process_source(self, source_id: int, owner_user_id: int,
                             progress: Optional[ProgressFn] = None) -> IngestionResult:
        source = self._store.sources.get(owner_user_id, source_id)
        if source is None:
            raise NotFoundError("الكتاب غير موجود")

        if source["status"] == COMPLETED_STATUS:
            return IngestionResult(
                source=source, status=COMPLETED_STATUS, chunk_count=0
            )

        result = IngestionResult(source=source, status=FAILED_STATUS)
        try:
            await self._run(source, progress, result)
        except Exception as exc:  # noqa: BLE001 - persist failures not crash
            safe = self._safe_error(exc)
            logger.exception(f"Knowledge ingestion failed for {source_id}: {safe}")
            self._store.sources.set_state(
                owner_user_id, source_id, FAILED_STATUS, result.state or "EXTRACT",
                error=safe, increment_retry=True,
            )
            result.status = FAILED_STATUS
            result.error = safe
        return result"""

new_process = """async def process_source(self, source_id: int, owner_user_id: int,
                             progress: Optional[ProgressFn] = None) -> IngestionResult:
        source = self._store.sources.get(owner_user_id, source_id)
        if source is None:
            raise NotFoundError("الكتاب غير موجود")

        if source["status"] == COMPLETED_STATUS:
            return IngestionResult(
                source=source, status=COMPLETED_STATUS, chunk_count=0
            )

        # Generate claim token for this ingestion run
        self._claim_token = self._generate_claim_token()
        claim_expires_at = self._claim_expires_at()

        # Attempt atomic claim acquisition
        claim_acquired = self._store.sources.atomic_claim(
            source_id=source_id,
            claim_token=self._claim_token,
            claim_expires_at=claim_expires_at,
        )
        if not claim_acquired:
            # Another worker is already processing this source
            logger.warning(f"Ingestion claim rejected for source {source_id}: already claimed")
            return IngestionResult(
                source=source, status=FAILED_STATUS, chunk_count=0,
                error="Source is already being processed by another worker"
            )

        # Start background claim renewal
        await self._start_claim_renewal(source_id)

        result = IngestionResult(source=source, status=FAILED_STATUS)
        try:
            await self._run(source, progress, result)
            # Success: clear the claim
            self._store.sources.clear_claim(source_id, self._claim_token)
        except Exception as exc:  # noqa: BLE001 - persist failures not crash
            safe = self._safe_error(exc)
            logger.exception(f"Knowledge ingestion failed for {source_id}: {safe}")
            self._store.sources.set_state(
                owner_user_id, source_id, FAILED_STATUS, result.state or "EXTRACT",
                error=safe, increment_retry=True,
            )
            # Cleanup partial data on failure (only if we still own the claim)
            self._store.cleanup_for_source(
                owner_user_id, source_id, claim_token=self._claim_token
            )
            # Release the claim (only if we still own it)
            self._store.sources.clear_claim(source_id, self._claim_token)
            result.status = FAILED_STATUS
            result.error = safe
        finally:
            # Stop claim renewal and ensure claim token is cleared
            await self._stop_claim_renewal()
            self._claim_token = None
        return result"""

if old_process in content:
    content = content.replace(old_process, new_process)
    print("7. Updated process_source method")
else:
    print("ERROR: Could not find process_source pattern")
    sys.exit(1)

# 4. Update _run method to remove redundant set_state call
old_run = """async def _run(self, source: Dict[str, Any],
               progress: Optional[ProgressFn], result: IngestionResult) -> None:
        owner = source["owner_user_id"]
        source_id = source["source_id"]
        self._store.sources.set_state(owner, source_id, _PROCESSING, "VALIDATE")

        # -- VALIDATE: storage + size --------------------------------------"""

new_run = """async def _run(self, source: Dict[str, Any],
               progress: Optional[ProgressFn], result: IngestionResult) -> None:
        owner = source["owner_user_id"]
        source_id = source["source_id"]
        # Note: atomic_claim already set status='processing' and ingestion_state='VALIDATE'

        # -- VALIDATE: storage + size --------------------------------------"""

if old_run in content:
    content = content.replace(old_run, new_run)
    print("8. Updated _run method")
else:
    print("ERROR: Could not find _run method pattern")
    sys.exit(1)

# 5. Update finalization to use complete_with_claim
old_final = """        )
        self._store.sources.set_state(
            owner, source_id, COMPLETED_STATUS, READY_STATE
        )
        await self._emit(
            progress, "READY", "جاهز", "✅ تمت فهرسة الكتاب بنجاح"
        )
        result.status = COMPLETED_STATUS
        result.chunk_count = len(chunks)"""

new_final = """        )
        # Atomically complete ingestion only if we still own the claim
        completed = self._store.sources.complete_with_claim(
            owner, source_id, self._claim_token, COMPLETED_STATUS, READY_STATE
        )
        if not completed:
            # Another worker has taken ownership; abort without publishing results
            result.status = FAILED_STATUS
            result.error = "claim ownership lost before finalization"
            return
        await self._emit(
            progress, "READY", "جاهز", "✅ تمت فهرسة الكتاب بنجاح"
        )
        result.status = COMPLETED_STATUS
        result.chunk_count = len(chunks)"""

if old_final in content:
    content = content.replace(old_final, new_final)
    print("9. Updated finalization to use complete_with_claim")
else:
    print("ERROR: Could not find finalization pattern")
    sys.exit(1)

# Write the modified ingestion.py
with open('app/knowledge/ingestion.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("All ingestion.py changes applied")

print("\n=== All H1 fixes applied successfully ===")