"""AUSTRO AI - Ingestion pipeline.

Drives a registered source through the ordered states
(UPLOAD -> VALIDATE -> SECURITY -> CHECKSUM -> FORMAT -> EXTRACT -> NORMALIZE
-> STRUCTURE -> SECTION -> CHUNK -> METADATA -> EMBED -> INDEX -> READY).

Every state is persisted so the UI can show live progress and a crashed job
can be resumed/retried. Heavy work (PDF parsing, embedding) runs in threads so
the asyncio loop (and Telegram) never blocks. A source is READY only after its
chunks are embedded and indexed - i.e. actually searchable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config.settings import Settings
from app.core.errors import NotFoundError, ValidationError
from app.knowledge.chunker import PreparedParagraph, SemanticChunker, split_paragraphs
from app.knowledge.cleaner import TextCleaner
from app.knowledge.embeddings import EmbeddingService
from app.knowledge.extractors import TextExtractor, detect_format
from app.knowledge.models import (
    COMPLETED_STATUS,
    EmbeddingRecord,
    FAILED_STATUS,
    IngestionResult,
    READY_STATE,
)
from app.knowledge.repositories import KnowledgeStore
from app.knowledge.storage import StorageService

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, str, str], Awaitable[None]]

_PROCESSING = "processing"


class IngestionPipeline:
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

    async def process_source(self, source_id: int, owner_user_id: int,
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
        return result

    # ------------------------------------------------------------------
    async def _run(self, source: Dict[str, Any],
                   progress: Optional[ProgressFn], result: IngestionResult) -> None:
        owner = source["owner_user_id"]
        source_id = source["source_id"]
        self._store.sources.set_state(owner, source_id, _PROCESSING, "VALIDATE")

        # -- VALIDATE: storage + size --------------------------------------
        await self._emit(progress, "VALIDATE", "التحقق من الملف", "جاري فحص الملف...")
        storage_key = source.get("storage_key")
        if not storage_key or not self._storage.exists(storage_key):
            raise ValidationError("الملف الأصلي غير موجود في التخزين")
        if int(source.get("file_size_bytes") or 0) > \
                self._settings.knowledge_max_file_size_mb * 1024 * 1024:
            raise ValidationError(
                f"حجم الملف يتجاوز الحد "
                f"({self._settings.knowledge_max_file_size_mb}MB)"
            )

        # -- SECURITY: read + checksum (tamper detection) ------------------
        await self._emit(progress, "SECURITY", "فحص الأمان", "جاري فحص سلامة الملف...")
        data = await asyncio.to_thread(
            self._storage.read, storage_key, source.get("checksum")
        )
        actual_checksum = hashlib.sha256(data).hexdigest()
        if source.get("checksum") and actual_checksum != source.get("checksum"):
            await self._emit(progress, "SECURITY", "فشل الفحص", "الملف يبدو معدلاً")
            raise ValidationError("الملف يبدو تالفاً أو معدلاً")

        # -- FORMAT ---------------------------------------------------------
        fmt = detect_format(source.get("file_name"), source.get("mime_type"))
        if fmt not in self._extractor._EXTRACTORS:  # noqa: SLF001 - registry
            raise ValidationError("صيغة الملف غير مدعومة")

        # -- EXTRACT --------------------------------------------------------
        await self._emit(progress, "EXTRACT", "استخراج النص", "جاري استخراج النص من الكتاب...")
        extracted = await asyncio.to_thread(self._extractor.extract, data, fmt)
        page_count = int(extracted.metadata.get("page_count", 0))
        if page_count > self._settings.knowledge_max_pages:
            raise ValidationError(
                f"عدد الصفحات يتجاوز الحد المسموح "
                f"({self._settings.knowledge_max_pages})"
            )
        if len(extracted.text) > self._settings.knowledge_max_chars:
            raise ValidationError("نص الكتاب كبير جداً للمعالجة")

        # -- NORMALIZE ------------------------------------------------------
        await self._emit(progress, "NORMALIZE", "تنظيف النص", "جاري تطبيع النص...")
        paragraphs: List[PreparedParagraph] = await asyncio.to_thread(
            split_paragraphs, extracted.text, self._cleaner, extracted.page_for_char
        )
        if not paragraphs:
            raise ValidationError("تعذر استخراج نص قابل للقراءة من الملف")
        normalized_text = "\n\n".join(p.body for p in paragraphs)
        result.state = "NORMALIZE"

        # -- STRUCTURE + SECTION + CHUNK -------------------------------------
        title = (source.get("title") or source.get("file_name") or "book").strip()
        chunked = await asyncio.to_thread(
            self._chunker.split, paragraphs, normalized_text
        )
        chunks = chunked["chunks"]
        sections_data = chunked["structure"]
        language = chunked["language"]
        await self._emit(
            progress, "SECTION", "تقسيم الأقسام",
            f"تم تحديد {len(sections_data)} قسم",
        )
        if not chunks or len(chunks) > self._settings.knowledge_max_chunks:
            raise ValidationError(
                "تعذر تقسيم الكتاب إلى مقاطع صالحة أو العدد كبير جداً"
            )

        # -- METADATA ---------------------------------------------------------
        await self._emit(progress, "METADATA", "إثراء البيانات",
                         "جاري ربط أقسام الكتاب...")
        metadata = self._metadata(source, extracted, language, sections_data)
        result.state = "METADATA"

        # -- DB WRITES (sections + document) ----------------------------------
        document_row = await asyncio.to_thread(
            self._index_sections, owner, source_id, title, language,
            normalized_text, extracted, sections_data,
        )
        document_id = document_row["document_id"]
        section_refs = document_row["section_refs"]
        result.document_id = document_id

        # -- EMBED -------------------------------------------------------------
        await self._emit(
            progress, "EMBED", "إنشاء التضمينات",
            f"جاري إنشاء التضمينات ({len(chunks)} مقطع)...",
        )
        texts = [c["content"] for c in chunks]
        vectors = await asyncio.to_thread(self._embeddings.embed_many, texts)
        if len(vectors) != len(texts):
            raise ValidationError("فشل إنشاء التضمينات")
        await self._emit(progress, "EMBED", "إنشاء التضمينات", "اكتمل.")

        # -- INDEX --------------------------------------------------------------
        await self._emit(progress, "INDEX", "الفهرسة", "جاري فهرسة المقاطع...")
        chunk_ids = await asyncio.to_thread(
            self._index_chunks, owner, source_id, document_id,
            section_refs, chunks, vectors,
        )
        if len(chunk_ids) != len(chunks):
            raise ValidationError("تعذر فهرسة جميع المقاطع")

        # -- READY (verify searchable) ------------------------------------------
        embedded_count = self._store.embeddings.count_for_source(
            source_id,
            model=self._embeddings.model(),
            version=self._embeddings.version(),
        )
        if embedded_count != len(chunks):
            raise ValidationError("فهرسة غير مكتملة، حاول المعالجة مجدداً")
        self._store.documents.complete(
            owner, document_id, total_sections=len(sections_data)
        )
        self._store.sources.set_title(
            owner, source_id, title=metadata["title"],
            author=metadata.get("author"), language=language,
            pages=page_count, char_count=len(normalized_text),
            metadata=metadata,
        )
        self._store.sources.set_state(
            owner, source_id, COMPLETED_STATUS, READY_STATE
        )
        await self._emit(
            progress, "READY", "جاهز", "✅ تمت فهرسة الكتاب بنجاح"
        )
        result.status = COMPLETED_STATUS
        result.chunk_count = len(chunks)

    # ------------------------------------------------------------------
    def _safe_error(self, exc: BaseException) -> str:
        if isinstance(exc, ValidationError):
            return str(exc)
        return f"نوع الخطأ: {type(exc).__name__}"

    def _metadata(self, source: Dict[str, Any], extracted, language: str,
                  sections_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        from app.knowledge.chunker import infer_title
        title = infer_title("\n\n".join(s["title"] for s in sections_data[:10]),
                            source.get("file_name")) if sections_data else \
            (source.get("title") or "كتاب")
        return {
            "title": title,
            "author": None,
            "file_name": source.get("file_name"),
            "format": source.get("file_format"),
            "pages": int(extracted.metadata.get("page_count", 0)),
            "section_count": len(sections_data),
            "language": language,
        }

    def _index_sections(self, owner: int, source_id: int, title: str,
                        language: str, normalized_text: str, extracted,
                        sections_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        document_id = self._store.documents.create(
            source_id=source_id,
            owner_user_id=owner,
            title=title,
            author=None,
            language=language,
            toc=sections_data,
            total_chars=len(normalized_text),
            total_pages=int(extracted.metadata.get("page_count", 0)),
            metadata={"paragraph_blocks": sections_data},
            version=1,
        )
        if document_id is None:
            raise ValidationError("تعذر إنشاء سجل المستند")
        section_refs: Dict[int, int] = {}
        for index, section in enumerate(sections_data):
            section_id = self._store.sections.create(
                document_id=document_id,
                owner_user_id=owner,
                source_id=source_id,
                level=section["level"],
                title=section["title"],
                order_index=section["order_index"],
                start_char=section["start_char"],
                end_char=section["end_char"],
            )
            if section_id is None:
                section_id = 0
            section_refs[index] = section_id
        return {"document_id": document_id, "section_refs": section_refs}

    def _index_chunks(self, owner: int, source_id: int, document_id: int,
                      section_refs: Dict[int, int], chunks: List[Dict[str, Any]],
                      vectors: List[List[float]]) -> List[int]:
        row_ids: List[int] = []
        for index, chunk in enumerate(chunks):
            section_index = chunk.get("section_id")
            section_id = section_refs.get(section_index) if section_index is not None else None
            if section_id == 0:
                section_id = None
            row_id = self._store.chunks.create(
                owner_user_id=owner,
                source_id=source_id,
                document_id=document_id,
                section_id=section_id,
                chunk_key=chunk["chunk_key"],
                content=chunk["content"],
                content_hash=chunk["content_hash"],
                token_count=chunk["token_count"],
                char_count=chunk["char_count"],
                page=chunk.get("page"),
                order_index=chunk["order_index"],
                metadata={
                    "start_char": chunk.get("start_char"),
                    "section_title": chunk.get("section_title"),
                    "paragraph_count": chunk.get("paragraph_count", 0),
                },
            )
            if row_id is None:
                row_id = self._existing_chunk_id(owner, chunk["chunk_key"])
            if row_id is None:
                continue
            row_ids.append(row_id)
            vector = vectors[index] if index < len(vectors) else []
            self._store.embeddings.save_many([EmbeddingRecord(
                owner_user_id=owner,
                source_id=source_id,
                chunk_row_id=row_id,
                model=self._embeddings.model(),
                version=self._embeddings.version(),
                dimensions=self._embeddings.dimensions(),
                vector=vector,
            )])
        return row_ids

    def _existing_chunk_id(self, owner: int, chunk_key: str) -> Optional[int]:
        try:
            with self._store._manager._lock:
                cursor = self._store._manager._get_connection().cursor()
                cursor.execute(
                    "SELECT chunk_id FROM knowledge_chunks "
                    "WHERE owner_user_id = ? AND chunk_key = ?",
                    (owner, chunk_key),
                )
                row = cursor.fetchone()
                return int(row["chunk_id"]) if row else None
        except Exception as e:  # noqa: BLE001
            logger.error(f"Knowledge existing chunk lookup failed: {e}")
            return None

    async def _emit(self, progress: Optional[ProgressFn], state: str,
                    title: str, message: str) -> None:
        if progress is not None:
            await progress(state, title, message)


__all__ = ["IngestionPipeline"]