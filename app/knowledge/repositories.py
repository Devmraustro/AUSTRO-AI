"""AUSTRO AI - Knowledge persistence layer.

Owner-scoped repositories over the shared SQLite manager. Every knowledge row
is bound to `owner_user_id` and every read filters by it, so no user can ever
see another user's books, chunks or evidence. All queries live here; the
services above never touch SQL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.connection import DatabaseManager
from app.database.dialect import DB_ERROR
from app.knowledge.models import (
    CANCELLED_STATUS,
    COMPLETED_STATUS,
    EmbeddingRecord,
    KnowledgeCollection,
    KnowledgeSection,
)

logger = logging.getLogger(__name__)


class _KnowledgeBase:
    def __init__(self, manager: DatabaseManager):
        self._manager = manager

    def _connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

    @staticmethod
    def _j(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False) if value else ""

    @staticmethod
    def _unjson(value: Any, default: Any = None) -> Any:
        if not value:
            return default if default is not None else {}
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}


class KnowledgeSourceRepository(_KnowledgeBase):
    _FIELDS = [
        "metadata_json",
    ]

    def create(self, *, owner_user_id: int, source_type: str, title: str,
               file_name: str, file_format: str, mime_type: str, file_size_bytes: int,
               checksum: str, storage_key: str, original_ref: Optional[str],
               metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Create a source row. Returns None when the file already exists
        (idempotent by owner + checksum)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT source_id FROM knowledge_sources "
                    "WHERE owner_user_id = ? AND checksum = ?",
                    (owner_user_id, checksum),
                )
                existing = cursor.fetchone()
                if existing:
                    return None
                cursor.execute(
                    "INSERT INTO knowledge_sources "
                    "(owner_user_id, source_type, title, author, file_name, "
                    "file_format, mime_type, file_size_bytes, checksum, "
                    "storage_key, original_ref, metadata_json, status, "
                    "ingestion_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, source_type, title, None, file_name,
                     file_format, mime_type, file_size_bytes, checksum,
                     storage_key, original_ref,
                     self._j(metadata or {}), "pending", "UPLOAD"),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge create source: {e}")
            return None

    def get(self, owner_user_id: int, source_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_sources "
                    "WHERE source_id = ? AND owner_user_id = ?",
                    (source_id, owner_user_id),
                )
                row = cursor.fetchone()
                return self._row(row)
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge get source: {e}")
            return None

    def list(self, owner_user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if status:
                    cursor.execute(
                        "SELECT * FROM knowledge_sources "
                        "WHERE owner_user_id = ? AND status = ? "
                        "ORDER BY created_at DESC",
                        (owner_user_id, status),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM knowledge_sources WHERE owner_user_id = ? "
                        "ORDER BY created_at DESC",
                        (owner_user_id,),
                    )
                return [self._row(row) for row in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge list sources: {e}")
            return []

    def set_state(self, owner_user_id: int, source_id: int,
                  status: str, state: str, error: Optional[str] = None,
                  increment_retry: bool = False) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                retry_sql = ", retry_count = retry_count + 1" if increment_retry else ""
                cursor.execute(
                    f"UPDATE knowledge_sources SET status = ?, ingestion_state = ?, "
                    f"error_message = ?, updated_at = ?{retry_sql} "
                    f"WHERE source_id = ? AND owner_user_id = ?",
                    (status, state, error, datetime.now().isoformat(),
                     source_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge set_state: {e}")
            return False

    def is_cancelled(self, owner_user_id: int, source_id: int) -> bool:
        row = self.get(owner_user_id, source_id)
        return bool(row and row["status"] == CANCELLED_STATUS)

    def set_title(self, owner_user_id: int, source_id: int, title: str,
                  author: Optional[str] = None, language: Optional[str] = None,
                  pages: int = 0, char_count: int = 0,
                  metadata: Optional[Dict[str, Any]] = None) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE knowledge_sources SET title = ?, author = ?, "
                    "language = ?, pages = ?, char_count = ?, metadata_json = ?, "
                    "updated_at = ? WHERE source_id = ? AND owner_user_id = ?",
                    (title, author, language, pages, char_count,
                     self._j(metadata or {}), datetime.now().isoformat(),
                     source_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge set_title: {e}")
            return False

    def delete(self, owner_user_id: int, source_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "DELETE FROM knowledge_sources "
                    "WHERE source_id = ? AND owner_user_id = ?",
                    (source_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge delete source: {e}")
            return False

    def _row(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = self._unjson(row["metadata_json"])
        data.pop("metadata_json", None)
        return data


class KnowledgeDocumentRepository(_KnowledgeBase):
    def create(self, *, source_id: int, owner_user_id: int, title: str,
               author: Optional[str], language: Optional[str], toc: List[Dict[str, Any]],
               total_chars: int, total_pages: int, metadata: Optional[Dict[str, Any]] = None,
               version: int = 1) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO knowledge_documents "
                    "(source_id, owner_user_id, version, title, author, language, "
                    "toc_json, total_chars, total_pages, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_id, owner_user_id, version, title, author, language,
                     self._j(toc), total_chars, total_pages,
                     self._j(metadata or {})),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge create document: {e}")
            return None

    def complete(self, owner_user_id: int, document_id: int,
                 total_sections: int, status: str = COMPLETED_STATUS) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE knowledge_documents SET total_sections = ?, status = ? "
                    "WHERE document_id = ? AND owner_user_id = ?",
                    (total_sections, status, document_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge complete document: {e}")
            return False

    def list_for_source(self, source_id: int) -> List[Dict[str, Any]]:
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
            logger.error(f"Database error in knowledge documents list_for_source: {e}")
            return []


class KnowledgeSectionRepository(_KnowledgeBase):
    def create(self, *, document_id: int, owner_user_id: int, source_id: int,
               level: int, title: str, order_index: int,
               start_char: Optional[int], end_char: Optional[int]) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO knowledge_sections "
                    "(document_id, owner_user_id, source_id, level, title, "
                    "order_index, start_char, end_char) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (document_id, owner_user_id, source_id, level, title,
                     order_index, start_char, end_char),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge create section: {e}")
            return None

    def list_for_document(self, document_id: int) -> List[KnowledgeSection]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_sections WHERE document_id = ? "
                    "ORDER BY order_index",
                    (document_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    rows.append(KnowledgeSection(
                        section_id=row["section_id"],
                        document_id=row["document_id"],
                        owner_user_id=row["owner_user_id"],
                        source_id=row["source_id"],
                        level=row["level"],
                        title=row["title"],
                        order_index=row["order_index"],
                        start_char=row["start_char"],
                        end_char=row["end_char"],
                        parent_section_id=row["parent_section_id"],
                    ))
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge list sections: {e}")
            return []

    def get(self, section_id: int) -> Optional[KnowledgeSection]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_sections WHERE section_id = ?",
                    (section_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return KnowledgeSection(
                    section_id=row["section_id"],
                    document_id=row["document_id"],
                    owner_user_id=row["owner_user_id"],
                    source_id=row["source_id"],
                    level=row["level"],
                    title=row["title"],
                    order_index=row["order_index"],
                    start_char=row["start_char"],
                    end_char=row["end_char"],
                    parent_section_id=row["parent_section_id"],
                )
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge get section: {e}")
            return None


class KnowledgeChunkRepository(_KnowledgeBase):
    def create(self, *, owner_user_id: int, source_id: int, document_id: int,
               section_id: Optional[int], chunk_key: str, content: str,
               content_hash: str, token_count: int, char_count: int,
               page: Optional[str], order_index: int,
               metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Insert a chunk. Returns None if an identical chunk already exists
        for this owner (idempotent re-ingestion)."""
        if not char_count:
            char_count = len(content)
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT chunk_id FROM knowledge_chunks "
                    "WHERE owner_user_id = ? AND chunk_key = ?",
                    (owner_user_id, chunk_key),
                )
                if cursor.fetchone():
                    return None
                cursor.execute(
                    "INSERT INTO knowledge_chunks "
                    "(owner_user_id, source_id, document_id, section_id, chunk_key, "
                    "content, content_hash, token_count, char_count, page, "
                    "order_index, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, source_id, document_id, section_id, chunk_key,
                     content, content_hash, token_count, char_count, page,
                     order_index, self._j(metadata or {})),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge create chunk: {e}")
            return None

    def count_for_source(self, source_id: int) -> int:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?",
                    (source_id,),
                )
                return int(cursor.fetchone()[0])
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge count chunks: {e}")
            return 0

    def list_candidates(self, owner_user_id: int, limit: int = 2000,
                        source_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Chunks eligible for retrieval (completed sources only)."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                q = (
                    "SELECT c.chunk_id, c.source_id, c.document_id, c.section_id, "
                    "c.content, c.page, c.order_index, c.metadata_json, c.chunk_key, "
                    "s.title, s.owner_user_id "
                    "FROM knowledge_chunks c "
                    "JOIN knowledge_sources s ON s.source_id = c.source_id "
                    "WHERE c.owner_user_id = ? AND s.status = ? "
                )
                params: List[Any] = [owner_user_id, COMPLETED_STATUS]
                if source_ids:
                    placeholders = ", ".join(["?"] * len(source_ids))
                    q += f" AND c.source_id IN ({placeholders}) "
                    params = [owner_user_id, COMPLETED_STATUS] + list(source_ids)
                q += " ORDER BY c.created_at DESC LIMIT ?"
                params.append(limit)
                cursor.execute(q, params)
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["metadata"] = self._unjson(row["metadata_json"])
                    data.pop("metadata_json", None)
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge list candidates: {e}")
            return []


class KnowledgeEmbeddingRepository(_KnowledgeBase):
    def save_many(self, records: List[EmbeddingRecord]) -> int:
        saved = 0
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                for record in records:
                    cursor.execute(
                        "INSERT OR REPLACE INTO knowledge_embeddings "
                        "(owner_user_id, source_id, chunk_row_id, model, version, "
                        "dimensions, vector_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (record.owner_user_id, record.source_id, record.chunk_row_id,
                         record.model, record.version, record.dimensions,
                         json.dumps(record.vector)),
                    )
                    saved += 1
                self._connection().commit()
                return saved
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge save embeddings: {e}")
            return saved

    def count_for_source(self, source_id: int, model: str, version: str) -> int:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM knowledge_embeddings "
                    "WHERE source_id = ? AND model = ? AND version = ?",
                    (source_id, model, version),
                )
                return int(cursor.fetchone()[0])
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge count embeddings: {e}")
            return 0

    def vectors_for_owner(self, owner_user_id: int, model: str, version: str,
                          source_ids: Optional[List[int]] = None) -> Dict[int, List[float]]:
        """Map chunk_row_id -> vector for the current embedding version."""
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                q = (
                    "SELECT chunk_row_id, vector_json, owner_user_id FROM "
                    "knowledge_embeddings WHERE owner_user_id = ? AND model = ? "
                    "AND version = ?"
                )
                params: List[Any] = [owner_user_id, model, version]
                if source_ids:
                    placeholders = ", ".join(["?"] * len(source_ids))
                    q += f" AND source_id IN ({placeholders})"
                    params += list(source_ids)
                cursor.execute(q, params)
                result: Dict[int, List[float]] = {}
                for row in cursor.fetchall():
                    try:
                        result[row["chunk_row_id"]] = json.loads(row["vector_json"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                return result
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge vectors_for_owner: {e}")
            return {}


class KnowledgeEventRepository(_KnowledgeBase):
    def log(self, *, owner_user_id: int, query: str, top_k: int,
            result_count: int, latency_ms: float, generator: str) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO knowledge_retrieval_events "
                    "(owner_user_id, query, top_k, result_count, latency_ms, generator) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (owner_user_id, query, top_k, result_count, latency_ms, generator),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge log event: {e}")
            return None

    def add_citations(self, event_id: int, citations: List[Dict[str, Any]]) -> int:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                for c in citations:
                    cursor.execute(
                        "INSERT INTO knowledge_citations "
                        "(event_id, chunk_row_id, source_id, title, section_title, "
                        "page, snippet, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (event_id, c["chunk_row_id"], c["source_id"], c["title"],
                         c.get("section_title"), c.get("page"),
                         c.get("snippet", ""), c.get("score", 0.0)),
                    )
                self._connection().commit()
                return len(citations)
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge add citations: {e}")
            return 0


class KnowledgeCollectionRepository(_KnowledgeBase):
    def create(self, owner_user_id: int, name: str, description: str = "") -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO knowledge_collections "
                    "(owner_user_id, name, description) VALUES (?, ?, ?)",
                    (owner_user_id, name, description),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge create collection: {e}")
            return None

    def list(self, owner_user_id: int) -> List[KnowledgeCollection]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM knowledge_collections WHERE owner_user_id = ? "
                    "ORDER BY created_at DESC",
                    (owner_user_id,),
                )
                return [
                    KnowledgeCollection(
                        collection_id=row["collection_id"],
                        owner_user_id=row["owner_user_id"],
                        name=row["name"],
                        description=row["description"] or "",
                        created_at=row["created_at"],
                    )
                    for row in cursor.fetchall()
                ]
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge list collections: {e}")
            return []

    def get(self, owner_user_id: int, collection_id: int) -> Optional[KnowledgeCollection]:
        for collection in self.list(owner_user_id):
            if collection.collection_id == collection_id:
                return collection
        return None

    def delete(self, owner_user_id: int, collection_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "DELETE FROM knowledge_collections "
                    "WHERE collection_id = ? AND owner_user_id = ?",
                    (collection_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge delete collection: {e}")
            return False

    def add_source(self, owner_user_id: int, collection_id: int, source_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT source_id FROM knowledge_sources "
                    "WHERE source_id = ? AND owner_user_id = ?",
                    (source_id, owner_user_id),
                )
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    "INSERT OR IGNORE INTO knowledge_collection_sources "
                    "(collection_id, source_id, owner_user_id) VALUES (?, ?, ?)",
                    (collection_id, source_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge add_source: {e}")
            return False

    def remove_source(self, owner_user_id: int, collection_id: int, source_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "DELETE FROM knowledge_collection_sources "
                    "WHERE collection_id = ? AND source_id = ? AND owner_user_id = ?",
                    (collection_id, source_id, owner_user_id),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge remove_source: {e}")
            return False

    def source_ids_for(self, owner_user_id: int, collection_ids: List[int]) -> List[int]:
        if not collection_ids:
            return []
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                placeholders = ", ".join(["?"] * len(collection_ids))
                cursor.execute(
                    "SELECT DISTINCT source_id FROM knowledge_collection_sources "
                    f"WHERE owner_user_id = ? AND collection_id IN ({placeholders})",
                    [owner_user_id] + list(collection_ids),
                )
                return [row["source_id"] for row in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge source_ids_for: {e}")
            return []

    def names_for_source(self, source_id: int) -> List[str]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT c.name FROM knowledge_collection_sources ccs "
                    "JOIN knowledge_collections c ON c.collection_id = ccs.collection_id "
                    "WHERE ccs.source_id = ?",
                    (source_id,),
                )
                return [row["name"] for row in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge names_for_source: {e}")
            return []


class KnowledgeStorageRepository(_KnowledgeBase):
    def register(self, *, owner_user_id: int, source_id: int, storage_key: str,
                 file_name: str, file_format: str, size_bytes: int,
                 checksum: str) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO knowledge_storage_files "
                    "(owner_user_id, source_id, storage_key, file_name, file_format, "
                    "size_bytes, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, source_id, storage_key, file_name, file_format,
                     size_bytes, checksum),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge register file: {e}")
            return False

    def mark_verified(self, source_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE knowledge_storage_files SET verified = TRUE WHERE source_id = ?",
                    (source_id,),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge mark verified: {e}")
            return False

    def verified(self, source_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT verified FROM knowledge_storage_files WHERE source_id = ?",
                    (source_id,),
                )
                row = cursor.fetchone()
                return bool(row and row["verified"])
        except DB_ERROR as e:
            logger.error(f"Database error in knowledge file verified: {e}")
            return False


# Aggregate facade so the knowledge service holds a single object.
class KnowledgeStore:
    def __init__(self, manager: DatabaseManager):
        self.sources = KnowledgeSourceRepository(manager)
        self.documents = KnowledgeDocumentRepository(manager)
        self.sections = KnowledgeSectionRepository(manager)
        self.chunks = KnowledgeChunkRepository(manager)
        self.embeddings = KnowledgeEmbeddingRepository(manager)
        self.events = KnowledgeEventRepository(manager)
        self.collections = KnowledgeCollectionRepository(manager)
        self.storage = KnowledgeStorageRepository(manager)
        self._manager = manager


__all__ = ["KnowledgeStore"]