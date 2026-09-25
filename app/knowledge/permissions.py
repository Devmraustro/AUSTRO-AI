"""AUSTRO AI - Knowledge access control.

Authorization is an application concern and is enforced here and in the
repositories before any data leaves the database. The LLM never decides who
may see what; every retrieval/scoring query already orders its rows by
`owner_user_id`.
"""

from __future__ import annotations

import logging

from app.core.errors import AuthorizationError, NotFoundError
from app.knowledge.repositories import KnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgePermissions:
    def __init__(self, store: KnowledgeStore):
        self._store = store

    def require_source(self, owner_user_id: int, source_id: int) -> dict:
        source = self._store.sources.get(owner_user_id, source_id)
        if source is None:
            raise NotFoundError("الكتاب غير موجود")
        return source

    def assert_owner(self, owner_user_id: int, source: dict) -> None:
        if str(source.get("owner_user_id")) != str(owner_user_id):
            raise AuthorizationError("لا تملك صلاحية الوصول لهذا الكتاب")


__all__ = ["KnowledgePermissions"]