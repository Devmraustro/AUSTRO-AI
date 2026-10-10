"""PostgreSQL: a failing purge of partial index rows is atomic and recoverable.

A PostgreSQL trigger makes the DELETE on ``knowledge_sections`` raise. The
purge runs in one transaction, so the rollback must keep every derived row;
once the trigger is removed the same purge must remove exactly this owner's
rows and leave the other owner untouched. Skipped when PostgreSQL is absent.
"""
import pytest

import test_repositories_pg as _pg
from test_knowledge_ownership import _world

globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})

DERIVED = ("knowledge_citations", "knowledge_embeddings", "knowledge_chunks",
           "knowledge_sections", "knowledge_documents")


@pytest.fixture()
def manager(pg_db):
    return pg_db._manager


@pytest.fixture()
def store(manager):
    from app.knowledge.repositories import KnowledgeStore
    return KnowledgeStore(manager)


def _exec(manager, sql: str) -> None:
    with manager._lock:
        conn = manager._get_connection()
        try:
            conn.cursor().execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()  # leave the shared connection usable for the caller
            raise


def _count(manager, owner: int, source_id: int) -> dict:
    counts = {}
    with manager._lock:
        cursor = manager._get_connection().cursor()
        for table in DERIVED:
            cursor.execute(f"SELECT COUNT(*) FROM {table} "  # nosec B608 - fixed names
                           "WHERE owner_user_id = ? AND source_id = ?"
                           if table != "knowledge_citations" else
                           "SELECT COUNT(*) FROM knowledge_citations c "
                           "JOIN knowledge_chunks k ON k.chunk_id = c.chunk_row_id "
                           "WHERE k.owner_user_id = ? AND k.source_id = ?",
                           (owner, source_id))
            counts[table] = cursor.fetchone()[0]
    return counts


def test_failed_purge_rolls_back_everything_on_postgres(store, manager):
    world = _world(store, 71, "pgfail")
    other = _world(store, 72, "pgother")
    before = _count(manager, 71, world["source"])
    assert before["knowledge_chunks"] == 1 and before["knowledge_documents"] == 1
    _exec(manager, "CREATE OR REPLACE FUNCTION austro_fail_delete() RETURNS trigger "
                   "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'simulated purge "
                   "failure'; END $$")
    _exec(manager, "CREATE TRIGGER austro_fail_sections BEFORE DELETE ON knowledge_sections "
                   "FOR EACH ROW EXECUTE FUNCTION austro_fail_delete()")
    try:
        assert store.purge_partial_index(71, world["source"]) is False
        assert _count(manager, 71, world["source"]) == before, "rollback keeps every row"
    finally:
        _exec(manager, "DROP TRIGGER IF EXISTS austro_fail_sections ON knowledge_sections")

    assert store.purge_partial_index(71, world["source"]) is True
    assert all(v == 0 for v in _count(manager, 71, world["source"]).values())
    assert _count(manager, 72, other["source"])["knowledge_chunks"] == 1


def test_child_insert_after_parent_deleted_is_rejected_by_foreign_key(store, manager):
    world = _world(store, 73, "pgrace")
    assert store.sources.delete(73, world["source"]) is True
    with pytest.raises(Exception) as exc:
        _exec(manager,
              "INSERT INTO knowledge_chunks (owner_user_id, source_id, document_id, "
              "section_id, chunk_key, content, content_hash, token_count, char_count, "
              "order_index) VALUES (73, {src}, {doc}, NULL, 'race', 'x', 'h', 1, 1, 0)"
              .format(src=world["source"], doc=world["document"]))
    assert "foreign key" in str(exc.value).lower()
    assert _count(manager, 73, world["source"])["knowledge_chunks"] == 0
