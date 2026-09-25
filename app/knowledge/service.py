"""AUSTRO AI - Knowledge application service (facade).

The single entry point the presentation layer (and tests) use to talk to the
knowledge engine. It owns the composition of repositories, storage,
ingestion, retrieval, RAG and collections, and enforces upload validation
before anything touches disk or the database.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from app.config.settings import Settings
from app.core.errors import ValidationError
from app.knowledge.collections import CollectionService
from app.knowledge.embeddings import EmbeddingService
from app.knowledge.extractors import detect_format
from app.knowledge.ingestion import IngestionPipeline
from app.knowledge.learning import LearningEngine, NotImplementedLearningEngine
from app.knowledge.models import IngestionResult, RAGAnswer
from app.knowledge.retrieval import RetrievalResult
from app.knowledge.permissions import KnowledgePermissions
from app.knowledge.rag import RAGService
from app.knowledge.repositories import KnowledgeStore
from app.knowledge.retrieval import RetrievalService
from app.knowledge.storage import StorageService

logger = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, storage: StorageService,
                 settings_: Settings, embeddings: EmbeddingService,
                 retrieval: RetrievalService, rag: RAGService,
                 collections: CollectionService, permissions: KnowledgePermissions,
                 pipeline: IngestionPipeline):
        self._store = store
        self._storage = storage
        self._settings = settings_
        self.embeddings = embeddings
        self.retrieval = retrieval
        self.rag = rag
        self.collections = collections
        self.permissions = permissions
        self.pipeline = pipeline

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    def register_upload(self, *, owner_user_id: int, file_name: str, data: bytes,
                        mime_type: Optional[str] = None, source_type: str = "book",
                        original_ref: Optional[str] = None) -> Dict[str, Any]:
        """Validate, store and register a raw file as a knowledge source.

        Returns {"source_id", "duplicate"} where duplicate=True means a file
        with the same checksum already exists for this owner (idempotent).
        """
        file_name = (file_name or "document").strip()
        fmt = detect_format(file_name, mime_type)
        size = len(data)
        max_bytes = self._settings.knowledge_max_file_size_mb * 1024 * 1024
        if size <= 0:
            raise ValidationError("الملف فارغ")
        if size > max_bytes:
            raise ValidationError(
                f"حجم الملف يتجاوز الحد المسموح "
                f"({self._settings.knowledge_max_file_size_mb}MB)"
            )

        checksum = hashlib.sha256(data).hexdigest()
        storage_key, checksum = self._storage.save(
            owner_user_id=owner_user_id,
            file_name=file_name,
            data=data,
            file_format=fmt,
            checksum=checksum,
        )
        title = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        source_id = self._store.sources.create(
            owner_user_id=owner_user_id,
            source_type=source_type,
            title=title[:120],
            file_name=file_name,
            file_format=fmt,
            mime_type=mime_type or "",
            file_size_bytes=size,
            checksum=checksum,
            storage_key=storage_key,
            original_ref=original_ref,
            metadata={"uploaded_at_utc": None},
        )
        if source_id is None:
            # Duplicate file for this owner: remove the copy just written.
            self._storage.delete(storage_key)
            existing = self._source_by_checksum(owner_user_id, checksum)
            return {"source_id": existing["source_id"] if existing else None,
                    "duplicate": True}

        self._storage.register_file(
            owner_user_id=owner_user_id,
            source_id=source_id,
            storage_key=storage_key,
            file_name=file_name,
            file_format=fmt,
            size_bytes=size,
            checksum=checksum,
        )
        logger.info(f"Registered knowledge source {source_id} for user {owner_user_id}")
        return {"source_id": source_id, "duplicate": False}

    async def process_source(self, owner_user_id: int, source_id: int,
                             progress=None) -> IngestionResult:
        return await self.pipeline.process_source(source_id, owner_user_id, progress)

    def duplicate_of(self, owner_user_id: int, source_id: int) -> bool:
        source = self._store.sources.get(owner_user_id, source_id)
        return bool(source and source["status"] == "completed")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def source(self, owner_user_id: int, source_id: int) -> Dict[str, Any]:
        return self.permissions.require_source(owner_user_id, source_id)

    def list_sources(self, owner_user_id: int,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._store.sources.list(owner_user_id, status)

    def processing_sources(self, owner_user_id: int) -> List[Dict[str, Any]]:
        return self._store.sources.list(owner_user_id, status="processing")

    def delete_source(self, owner_user_id: int, source_id: int) -> bool:
        source = self.permissions.require_source(owner_user_id, source_id)
        self._storage.delete(source.get("storage_key"))
        return self._store.sources.delete(owner_user_id, source_id)

    def counts(self, owner_user_id: int) -> Dict[str, int]:
        sources = self._store.sources.list(owner_user_id)
        return {
            "total": len(sources),
            "ready": sum(1 for s in sources if s["status"] == "completed"),
            "processing": sum(1 for s in sources if s["status"] == "processing"),
            "failed": sum(1 for s in sources if s["status"] == "failed"),
        }

    def settings_info(self) -> Dict[str, Any]:
        return {
            "max_file_size_mb": self._settings.knowledge_max_file_size_mb,
            "max_pages": self._settings.knowledge_max_pages,
            "max_chars": self._settings.knowledge_max_chars,
            "chunk_size": self._settings.knowledge_chunk_size,
            "chunk_overlap": self._settings.knowledge_chunk_overlap,
            "embedding_model": self.embeddings.model(),
            "embedding_version": self.embeddings.version(),
            "dimensions": self.embeddings.dimensions(),
            "top_k": self._settings.knowledge_retrieval_top_k,
            "min_score": self._settings.knowledge_retrieval_min_score,
        }

    # ------------------------------------------------------------------
    # Retrieval + RAG
    # ------------------------------------------------------------------
    def search(self, owner_user_id: int, question: str,
               limit: Optional[int] = None,
               collections: Optional[List[int]] = None) -> RetrievalResult:
        return self.retrieval.retrieve(owner_user_id, question,
                                       top_k=limit, collections=collections)

    async def answer(self, owner_user_id: int, question: str,
                     collections: Optional[List[int]] = None) -> RAGAnswer:
        return await self.rag.answer(owner_user_id, question,
                                     collections=collections)

    # ------------------------------------------------------------------
    # Learning engine (future interfaces)
    # ------------------------------------------------------------------
    def learning(self) -> LearningEngine:
        return NotImplementedLearningEngine()

    def _source_by_checksum(self, owner_user_id: int, checksum: str) -> Optional[Dict[str, Any]]:
        for source in self._store.sources.list(owner_user_id):
            if source.get("checksum") == checksum:
                return source
        return None


__all__ = ["KnowledgeService"]