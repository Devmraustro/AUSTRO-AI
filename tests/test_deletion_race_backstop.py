"""Race backstop: a child row cannot be written after its parent is deleted.

Repository validators check the parent before INSERT. Between that check and
the INSERT another request may delete the parent. The database foreign keys
must then reject the child on their own. The ids used below are the real
document of an indexed source, captured before the delete. Raw SQL on SQLite;
the PostgreSQL side is covered in tests/test_ingestion_purge_failure_pg.py.
"""

from __future__ import annotations

import pytest

import database
from app.core.container import build_container
from tests.test_knowledge_cleanup import OWNER, _register


@pytest.fixture()
def service():
    return build_container().knowledge


def _raw(sql: str, params=(), fetch=False):
    manager = database.db._manager
    with manager._lock:
        conn = manager._get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        conn.commit()
    return rows


@pytest.mark.asyncio
async def test_chunk_insert_after_parent_source_deleted_is_rejected(service):
    source_id = await _register(service, OWNER)
    assert (await service.process_source(OWNER, source_id)).status == "completed"
    row = _raw("SELECT document_id FROM knowledge_documents WHERE source_id = ?",
               (source_id,), fetch=True)[0]
    document_id = row["document_id"] if isinstance(row, dict) else row[0]

    assert service.delete_source(OWNER, source_id) is True

    with pytest.raises(Exception) as exc:
        _raw("INSERT INTO knowledge_chunks (owner_user_id, source_id, document_id, "
             "section_id, chunk_key, content, content_hash, token_count, char_count, "
             "order_index) VALUES (?, ?, ?, NULL, 'race', 'x', 'h', 1, 1, 0)",
             (OWNER, source_id, document_id))
    assert "FOREIGN KEY" in str(exc.value).upper()
    left = _raw("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?",
                (source_id,), fetch=True)[0]
    assert (left["COUNT(*)"] if isinstance(left, dict) else left[0]) == 0
