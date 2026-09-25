"""AUSTRO AI - Retrieval service.

Hybrid, owner-scoped retrieval: keyword (term overlap) + semantic (cosine on
the active embedding model) + metadata (title / section bonus). Candidate rows
are pre-filtered to the requesting owner and to READY sources only, then
deduped, ranked and capped at `top_k`. Every retrieval is logged together with
its citations so the system can be audited and evaluated.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.config.settings import Settings
from app.knowledge.cleaner import TextCleaner
from app.knowledge.embeddings import EmbeddingService, cosine_similarity
from app.knowledge.models import Citation, RetrievedChunk
from app.knowledge.repositories import KnowledgeStore

logger = logging.getLogger(__name__)

_WEIGHT_KEYWORD = 0.55
_WEIGHT_SEMANTIC = 0.35
_METADATA_BONUS_CAP = 0.20


@dataclass
class RetrievalResult:
    query: str
    chunks: List[RetrievedChunk] = field(default_factory=list)
    event_id: Optional[int] = None
    generator: str = ""
    latency_ms: float = 0.0
    candidates_scanned: int = 0

    @property
    def grounded(self) -> bool:
        return bool(self.chunks)


class RetrievalService:
    """Scores candidate chunks and returns ranked, citable evidence."""

    def __init__(self, store: KnowledgeStore, embeddings: EmbeddingService,
                 settings_: Settings, cleaner: Optional[TextCleaner] = None):
        self._store = store
        self._embeddings = embeddings
        self._settings = settings_
        self._cleaner = cleaner or TextCleaner()

    # -- public API ---------------------------------------------------------
    def retrieve(self, owner_user_id: int, query: str, top_k: Optional[int] = None,
                 collections: Optional[List[int]] = None) -> RetrievalResult:
        started = time.perf_counter()
        top_k = top_k or self._settings.knowledge_retrieval_top_k

        source_ids = None
        if collections:
            source_ids = self._store.collections.source_ids_for(owner_user_id, collections)

        candidates = self._store.chunks.list_candidates(
            owner_user_id, limit=2000, source_ids=source_ids,
        )
        if not candidates:
            result = RetrievalResult(query=query, generator=self._embeddings.model())
            self._log_event(owner_user_id, query, top_k, result)
            result.latency_ms = (time.perf_counter() - started) * 1000
            return result

        vectors = self._store.embeddings.vectors_for_owner(
            owner_user_id,
            model=self._embeddings.model(),
            version=self._embeddings.version(),
            source_ids=source_ids,
        )
        query_vector = self._embeddings.embed_query(query)
        query_tokens = set(self._cleaner.tokens(query))

        ranked: List[RetrievedChunk] = []
        seen: Dict[str, float] = {}
        for candidate in candidates:
            content = candidate["content"] or ""
            content_tokens = set(self._cleaner.tokens(content))

            keyword = self._keyword_score(query_tokens, content_tokens)
            semantic = 0.0
            chunk_vector = vectors.get(candidate["chunk_id"])
            if chunk_vector:
                semantic = cosine_similarity(query_vector, chunk_vector)
            metadata = self._metadata_bonus(
                query_tokens, candidate.get("title") or "",
                candidate.get("metadata", {}).get("section_title", ""),
            )
            score = (
                keyword * _WEIGHT_KEYWORD
                + semantic * _WEIGHT_SEMANTIC
                + min(metadata, _METADATA_BONUS_CAP)
            )
            if score < self._settings.knowledge_retrieval_min_score:
                continue

            duplicate = seen.get(candidate["chunk_key"])
            if duplicate is not None:
                if duplicate < score:
                    seen[candidate["chunk_key"]] = score
                    for idx, item in enumerate(ranked):
                        if item.chunk_row_id == candidate["chunk_id"]:
                            ranked.pop(idx)
                            break
                else:
                    continue

            seen[candidate["chunk_key"]] = score
            section = None
            if candidate.get("section_id"):
                section = self._store.sections.get(candidate["section_id"])
            ranked.append(RetrievedChunk(
                chunk_row_id=candidate["chunk_id"],
                source_id=candidate["source_id"],
                content=content,
                score=round(score, 4),
                title=candidate.get("title") or "مصدر",
                section_title=section.title if section else None,
                page=candidate.get("page"),
                collection_names=[],
            ))

        ranked.sort(key=lambda item: item.score, reverse=True)
        ranked = ranked[:top_k]

        result = RetrievalResult(
            query=query,
            chunks=ranked,
            generator=self._embeddings.model(),
            candidates_scanned=len(candidates),
        )
        result.event_id = self._log_event(owner_user_id, query, top_k, result)
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    def as_citations(self, chunks: List[RetrievedChunk]) -> List[Citation]:
        return [
            Citation(
                source_title=chunk.title,
                source_id=chunk.source_id,
                section_title=chunk.section_title,
                page=chunk.page,
                snippet=chunk.content[:220],
            )
            for chunk in chunks
        ]

    # -- internals ----------------------------------------------------------
    def _keyword_score(self, query_tokens: set, content_tokens: set) -> float:
        if not query_tokens:
            return 0.0
        matched = len(query_tokens & content_tokens)
        return matched / len(query_tokens)

    def _metadata_bonus(self, query_tokens: set, title: str,
                        section_title: str) -> float:
        bonus = 0.0
        for value in (title, section_title):
            if not value:
                continue
            value_tokens = set(self._cleaner.tokens(value))
            if value_tokens & query_tokens:
                bonus += 0.10
        return bonus

    def _log_event(self, owner_user_id: int, query: str, top_k: int,
                   result: RetrievalResult) -> Optional[int]:
        event_id = self._store.events.log(
            owner_user_id=owner_user_id,
            query=query,
            top_k=top_k,
            result_count=len(result.chunks),
            latency_ms=result.latency_ms,
            generator=result.generator,
        )
        if event_id is not None and result.chunks:
            self._store.events.add_citations(event_id, [
                {
                    "chunk_row_id": chunk.chunk_row_id,
                    "source_id": chunk.source_id,
                    "title": chunk.title,
                    "section_title": chunk.section_title,
                    "page": chunk.page,
                    "snippet": chunk.content[:220],
                    "score": chunk.score,
                }
                for chunk in result.chunks
            ])
        return event_id


__all__ = ["RetrievalResult", "RetrievalService"]