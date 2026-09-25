"""AUSTRO AI - Book-to-course (Phase E).

Builds a learning course from an ingested Knowledge source. The course
objectives are grounded in the book's OWN structure (section titles + real
chunk content). Optional LLM concept extraction is schema-validated; on
invalid output we fall back to section titles. Lessons generated for the
course are loaded with real, cited evidence slices - never fabricated
chapter/page numbers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.domain.ai import AIRequest
from app.knowledge.repositories import KnowledgeStore
from app.learning.repositories import LearningStore
from app.learning.schemas import (
    CONCEPT_EXTRACTION_FIELDS,
    extract_json,
    validate_schema,
    valid_concepts,
)

logger = logging.getLogger(__name__)


class BookToCourse:
    def __init__(self, learning_store: LearningStore, knowledge_store: KnowledgeStore,
                 ai=None, settings: Optional[Dict[str, Any]] = None):
        self._store = learning_store
        self._knowledge = knowledge_store
        self._ai = ai
        self._settings = settings or {}

    # -- chapter map --------------------------------------------------------
    def chapter_map(self, owner_user_id: int, source_id: int) -> Dict[str, Any]:
        """{source_id, title, chapters: [{section_id, title, order_index}]}"""
        source = self._knowledge.sources.get(owner_user_id, source_id)
        if source is None:
            return {}
        documents = self._knowledge.documents.list_for_source(source_id)
        document_id = documents[0]["document_id"] if documents else None
        chapters: List[Dict[str, Any]] = []
        if document_id:
            sections = self._knowledge.sections.list_for_document(document_id)
            for section in sections:
                title = section.title or ""
                if not title or title == "بدون عنوان":
                    continue
                chapters.append({
                    "section_id": section.section_id,
                    "title": title,
                    "order_index": section.order_index or len(chapters),
                })
        return {
            "source_id": source_id,
            "title": source.get("title") or "",
            "chapters": chapters,
        }

    # -- concepts -----------------------------------------------------------
    def concepts(self, owner_user_id: int, source_id: int,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Grounding concept list from the source itself."""
        source = self._knowledge.sources.get(owner_user_id, source_id)
        if source is None:
            return []
        title = source.get("title") or ""

        if self._use_llm():
            chunks = self._knowledge.chunks.list_candidates(
                owner_user_id, source_ids=[source_id], limit=120,
            )
            excerpts = [c.get("content", "")[:400] for c in chunks[:12]]
            concepts = self._llm_concepts(owner_user_id, title, excerpts)
            if concepts:
                return concepts[:limit] if limit else concepts

        return self._fallback_concepts(owner_user_id, source_id, limit)

    def _fallback_concepts(self, owner_user_id: int, source_id: int,
                           limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Concepts = section titles of the document (deterministic)."""
        documents = self._knowledge.documents.list_for_source(source_id)
        document_id = documents[0]["document_id"] if documents else None
        if document_id is None:
            document_id = self._create_minimal_document(owner_user_id, source_id)
        sections = self._knowledge.sections.list_for_document(document_id)
        concepts = []
        for section in sections:
            title = (section.title or "").strip()
            if title and title != "بدون عنوان":
                concepts.append({"title": title})
        if not concepts:
            concepts = [{"title": self._knowledge.sources.get(owner_user_id, source_id)
                         .get("title") or "الدورة"}]
        if limit:
            concepts = concepts[:limit]
        return concepts

    def _create_minimal_document(self, owner_user_id: int, source_id: int) -> Optional[int]:
        source = self._knowledge.sources.get(owner_user_id, source_id)
        if source is None:
            return None
        return self._knowledge.documents.create(
            source_id=source_id, owner_user_id=owner_user_id,
            title=source.get("title") or "document",
            author=source.get("author"), language=source.get("language"),
            toc=[], total_chars=0, total_pages=0,
        )

    def _llm_concepts(self, owner_user_id: int, title: str,
                      excerpts: List[str]) -> List[Dict[str, Any]]:
        request = AIRequest(
            capability="concept_extraction",
            prompt=(
                f"استخرج مفاهيم تعلم أساسية من الكتاب «{title}» بناءً على هذه "
                f"المقاطع:\n" + "\n".join(f"- {e}" for e in excerpts) +
                "\nأعد JSON: {\"concepts\": [{\"title\": \"\"}]}",
            ),
            system_prompt="أنت محلل محتوى. أعد JSON فقط.",
            max_tokens=800, temperature=0.2, user_id=owner_user_id,
            data={"title": title},
        )
        try:
            response = asyncio.new_event_loop().run_until_complete(
                self._ai.generate(request)
            )
            parsed = extract_json(response.text)
            if validate_schema(parsed, CONCEPT_EXTRACTION_FIELDS) or \
                    not valid_concepts(parsed):
                logger.warning("LLM concept extraction failed validation, using sections")
                return []
            return list(parsed.get("concepts", []))
        except Exception as exc:  # pragma: no cover - fallback safety
            logger.warning(f"LLM concept extraction failed: {exc}")
            return []

    def _use_llm(self) -> bool:
        if not bool(self._settings.get("learning_use_llm", False)):
            return False
        try:
            return bool(self._ai and self._ai.provider and self._ai.provider.is_available)
        except Exception:
            return False

    # -- course creation ----------------------------------------------------
    def create_course(self, *, owner_user_id: int, source_id: int,
                      title: Optional[str] = None,
                      mode: str = "READ") -> Optional[Dict[str, Any]]:
        source = self._knowledge.sources.get(owner_user_id, source_id)
        if source is None or source.get("status") != "completed":
            return None
        course_title = title or f"دورة من كتاب «{source.get('title') or ''}»"
        concepts = self.concepts(owner_user_id, source_id)
        if not concepts:
            return None

        goal_id = self._store.goals.create(
            owner_user_id=owner_user_id, kind="long_term",
            title=course_title, description=source.get("title") or "",
        )
        if goal_id is None:
            return None

        objective_ids: List[int] = []
        previous = None
        for index, concept in enumerate(concepts):
            concept_title = concept.get("title") or f"مفهوم {index + 1}"
            description = concept.get("description") or ""
            difficulty = "easy" if index < len(concepts) * 0.33 else (
                "hard" if index > len(concepts) * 0.66 else "medium")
            prerequisites = [previous] if previous is not None else []
            objective_id = self._store.objectives.create(
                owner_user_id=owner_user_id, goal_id=goal_id,
                title=concept_title, description=description,
                difficulty=difficulty, prerequisites=prerequisites,
            )
            if objective_id is not None:
                objective_ids.append(objective_id)
                previous = objective_id

        if not objective_ids:
            return None

        curriculum_id = self._store.curricula.create(
            owner_user_id=owner_user_id, goal_id=goal_id,
            title=course_title, mode=mode,
            modules=_modules(objective_ids),
        )
        self._store.events.log(
            owner_user_id, "course_created", None, None,
            {"source_id": source_id, "goal_id": goal_id,
             "curriculum_id": curriculum_id, "objectives": len(objective_ids)},
        )
        return {
            "goal_id": goal_id,
            "curriculum_id": curriculum_id,
            "objectives": objective_ids,
            "title": course_title,
        }

    # -- evidence for grounded lessons ---------------------------------------
    def evidence(self, owner_user_id: int, source_id: int, query: str,
                 top_k: int = 3) -> List[Dict[str, Any]]:
        """Real, cited slices for a lesson (deterministic token-overlap rank)."""
        source = self._knowledge.sources.get(owner_user_id, source_id)
        if source is None:
            return []
        chunks = self._knowledge.chunks.list_candidates(
            owner_user_id, source_ids=[source_id], limit=2000,
        )
        documents = self._knowledge.documents.list_for_source(source_id)
        document_id = documents[0]["document_id"] if documents else None
        sections_by_id = {}
        if document_id:
            for section in self._knowledge.sections.list_for_document(document_id):
                sections_by_id[section.section_id] = section

        query_tokens = _tokens(query)
        scored = []
        for chunk in chunks:
            chunk_tokens = _tokens(chunk.get("content", ""))
            overlap = len(query_tokens & chunk_tokens)
            if overlap <= 0:
                continue
            section = sections_by_id.get(chunk.get("section_id"))
            scored.append({
                "content": chunk.get("content", ""),
                "section_title": getattr(section, "title", None) if section else None,
                "page": chunk.get("page"),
                "source_id": chunk.get("source_id"),
                "title": chunk.get("title") or source.get("title") or "",
                "score": overlap / (query_tokens.bit_length() or 1),
            })
        scored.sort(key=lambda c: (c["score"], len(c["content"])), reverse=True)
        return scored[:top_k]


def _tokens(text: str) -> set:
    import re
    return set(re.findall(r"[\w\u0600-\u06FF]{2,}", (text or "").lower()))


def _modules(objective_ids: List[int]) -> List[Dict[str, Any]]:
    modules = []
    for index in range(0, len(objective_ids), 4):
        module_batch = objective_ids[index:index + 4]
        modules.append({
            "module_index": index // 4 + 1,
            "title": f"الوحدة {index // 4 + 1}",
            "objective_ids": module_batch,
        })
    if not modules:
        modules.append({"module_index": 1, "title": "الوحدة 1",
                        "objective_ids": []})
    return modules


__all__ = ["BookToCourse", "_tokens", "_modules"]