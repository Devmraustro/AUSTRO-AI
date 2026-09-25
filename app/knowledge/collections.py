"""AUSTRO AI - Knowledge collections service (owner-scoped)."""

from __future__ import annotations

import logging
from typing import Dict, List

from app.core.errors import NotFoundError, ValidationError
from app.knowledge.models import KnowledgeCollection
from app.knowledge.repositories import KnowledgeStore

logger = logging.getLogger(__name__)


class CollectionService:
    """Create, list and manage collections of knowledge sources."""

    def __init__(self, store: KnowledgeStore):
        self._store = store

    def create(self, owner_user_id: int, name: str, description: str = "") -> int:
        name = (name or "").strip()
        if not name or len(name) > 40:
            raise ValidationError("اسم المجموعة يجب أن يكون بين 1 و 40 حرفاً")
        collection_id = self._store.collections.create(owner_user_id, name, description)
        if collection_id is None:
            raise ValidationError("يوجد مجموعة بهذا الاسم بالفعل")
        return collection_id

    def list(self, owner_user_id: int) -> List[KnowledgeCollection]:
        return self._store.collections.list(owner_user_id)

    def get(self, owner_user_id: int, collection_id: int) -> KnowledgeCollection:
        collection = self._store.collections.get(owner_user_id, collection_id)
        if collection is None:
            raise NotFoundError("المجموعة غير موجودة")
        return collection

    def delete(self, owner_user_id: int, collection_id: int) -> bool:
        self.get(owner_user_id, collection_id)
        return self._store.collections.delete(owner_user_id, collection_id)

    def add_source(self, owner_user_id: int, collection_id: int, source_id: int) -> bool:
        self.get(owner_user_id, collection_id)
        if not self._store.sources.get(owner_user_id, source_id):
            raise NotFoundError("الكتاب غير موجود")
        return self._store.collections.add_source(owner_user_id, collection_id, source_id)

    def remove_source(self, owner_user_id: int, collection_id: int, source_id: int) -> bool:
        self.get(owner_user_id, collection_id)
        return self._store.collections.remove_source(owner_user_id, collection_id, source_id)

    def sources_in(self, owner_user_id: int, collection_id: int) -> List[Dict]:
        source_ids = self._store.collections.source_ids_for(
            owner_user_id, [collection_id]
        )
        books = []
        for source_id in source_ids:
            source = self._store.sources.get(owner_user_id, source_id)
            if source:
                books.append(source)
        return books


__all__ = ["CollectionService"]