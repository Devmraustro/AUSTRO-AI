"""Cross-owner access to knowledge sources, chunks, events, collections and storage.

Owner A (1) and owner B (2) each own a full knowledge graph (source, document,
section, chunk, retrieval event, collection, storage file). B then uses A's IDs
with B's identity. Each call must return no A data, write nothing, and report
failure. Raw SQL confirms the stored rows after each call. Same-owner calls
must still succeed, so validation cannot be a blanket refusal.

The bodies are shared with tests/test_knowledge_ownership_pg.py, which runs them
on PostgreSQL 16 by replacing only the ``manager`` fixture.
"""

from __future__ import annotations

import pytest

import database
from app.knowledge.models import EmbeddingRecord
from app.knowledge.repositories import KnowledgeStore

OWNER_A = 1
OWNER_B = 2
MODEL = "local"
VERSION = "1"


def _raw(manager, sql: str, params=()):
    with manager._lock:
        conn = manager._get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


def _scalar(manager, sql: str, params=()):
    return _raw(manager, sql, params)[0][0]


@pytest.fixture()
def manager():
    return database.db._manager


@pytest.fixture()
def store(manager):
    return KnowledgeStore(manager)


def _world(store, owner: int, tag: str):
    """Build one owner's complete graph and return its IDs."""
    source_id = store.sources.create(
        owner_user_id=owner, source_type="book", title=f"{tag} source",
        file_name=f"{tag}.txt", file_format="txt", mime_type="text/plain",
        file_size_bytes=10, checksum=f"sum-{tag}", storage_key=f"key/{tag}",
        original_ref=None)
    document_id = store.documents.create(
        source_id=source_id, owner_user_id=owner, title=f"{tag} doc", author=None,
        language="en", toc=[], total_chars=10, total_pages=1)
    section_id = store.sections.create(
        document_id=document_id, owner_user_id=owner, source_id=source_id,
        level=1, title=f"{tag} section", order_index=0, start_char=0, end_char=10)
    chunk_id = store.chunks.create(
        owner_user_id=owner, source_id=source_id, document_id=document_id,
        section_id=section_id, chunk_key=f"{tag}-c1", content=f"{tag} secret text",
        content_hash=f"h-{tag}", token_count=2, char_count=10, page=None, order_index=0)
    event_id = store.events.log(owner_user_id=owner, query=f"{tag} query", top_k=3,
                                result_count=1, latency_ms=1.0, generator="local")
    collection_id = store.collections.create(owner, f"{tag} collection")
    for value in (source_id, document_id, section_id, chunk_id, event_id, collection_id):
        assert isinstance(value, int) and value > 0
    return {"source": source_id, "document": document_id, "section": section_id,
            "chunk": chunk_id, "event": event_id, "collection": collection_id,
            "tag": tag}


@pytest.fixture()
def a(store):
    return _world(store, OWNER_A, "alpha")


@pytest.fixture()
def b(store):
    return _world(store, OWNER_B, "bravo")


def _citation(chunk_id: int, source_id: int) -> dict:
    return {"chunk_row_id": chunk_id, "source_id": source_id, "title": "t",
            "section_title": "s", "page": None, "snippet": "x", "score": 0.5}


# --- Chunks and embeddings -------------------------------------------------

def test_chunk_count_is_owner_scoped(store, a, b):
    assert store.chunks.count_for_source(OWNER_A, a["source"]) == 1
    assert store.chunks.count_for_source(OWNER_B, a["source"]) == 0


def test_embedding_count_is_owner_scoped(store, manager, a):
    assert store.embeddings.save_many([EmbeddingRecord(
        owner_user_id=OWNER_A, source_id=a["source"], chunk_row_id=a["chunk"],
        model=MODEL, version=VERSION, dimensions=2, vector=[0.1, 0.2])]) == 1
    assert store.embeddings.count_for_source(OWNER_A, a["source"], MODEL, VERSION) == 1
    assert store.embeddings.count_for_source(OWNER_B, a["source"], MODEL, VERSION) == 0


def test_embedding_for_other_owner_chunk_is_rejected(store, manager, a, b):
    """B writes an embedding for A's chunk using B's identity."""
    saved = store.embeddings.save_many([EmbeddingRecord(
        owner_user_id=OWNER_B, source_id=a["source"], chunk_row_id=a["chunk"],
        model=MODEL, version=VERSION, dimensions=2, vector=[0.9, 0.9])])
    assert saved == 0
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_embeddings "
                   "WHERE owner_user_id = ?", (OWNER_B,)) == 0


def test_embedding_batch_with_one_foreign_chunk_writes_nothing(store, manager, a, b):
    """A mixed batch (B's own chunk plus A's chunk) is rejected as a whole."""
    saved = store.embeddings.save_many([
        EmbeddingRecord(owner_user_id=OWNER_B, source_id=b["source"],
                        chunk_row_id=b["chunk"], model=MODEL, version=VERSION,
                        dimensions=2, vector=[0.1, 0.1]),
        EmbeddingRecord(owner_user_id=OWNER_B, source_id=a["source"],
                        chunk_row_id=a["chunk"], model=MODEL, version=VERSION,
                        dimensions=2, vector=[0.2, 0.2]),
    ])
    assert saved == 0
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_embeddings "
                   "WHERE owner_user_id = ?", (OWNER_B,)) == 0


def test_embedding_with_mismatched_source_is_rejected(store, manager, b):
    """B's own chunk, but the record names a different source."""
    other = _world(store, OWNER_B, "charlie")
    saved = store.embeddings.save_many([EmbeddingRecord(
        owner_user_id=OWNER_B, source_id=other["source"], chunk_row_id=b["chunk"],
        model=MODEL, version=VERSION, dimensions=2, vector=[0.3, 0.3])])
    assert saved == 0


# --- Retrieval events and citations ----------------------------------------

def test_citations_on_other_owner_event_are_rejected(store, manager, a, b):
    before = _scalar(manager, "SELECT COUNT(*) FROM knowledge_citations")
    written = store.events.add_citations(
        OWNER_B, a["event"], [_citation(b["chunk"], b["source"])])
    assert written == 0
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_citations") == before


def test_citation_to_other_owner_chunk_is_rejected(store, manager, a, b):
    before = _scalar(manager, "SELECT COUNT(*) FROM knowledge_citations")
    written = store.events.add_citations(
        OWNER_B, b["event"], [_citation(a["chunk"], a["source"])])
    assert written == 0
    assert _cit_count_for_event(manager, b["event"]) == 0
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_citations") == before


def test_citation_with_mismatched_source_is_rejected(store, manager, b):
    before = _cit_count_for_event(manager, b["event"])
    written = store.events.add_citations(
        OWNER_B, b["event"], [_citation(b["chunk"], 999999)])
    assert written == 0
    assert _cit_count_for_event(manager, b["event"]) == before


def test_citations_same_owner_succeed(store, manager, a):
    event_id = store.events.log(owner_user_id=OWNER_A, query="again", top_k=3,
                                result_count=1, latency_ms=1.0, generator="local")
    written = store.events.add_citations(
        OWNER_A, event_id, [_citation(a["chunk"], a["source"])])
    assert written == 1
    assert _cit_count_for_event(manager, event_id) == 1


def _cit_count_for_event(manager, event_id: int) -> int:
    return _scalar(manager, "SELECT COUNT(*) FROM knowledge_citations WHERE event_id = ?",
                   (event_id,))


# --- Collections -----------------------------------------------------------

def test_b_cannot_add_source_to_a_collection(store, manager, a, b):
    assert store.collections.add_source(OWNER_B, a["collection"], b["source"]) is False
    assert store.collections.add_source(OWNER_B, a["collection"], a["source"]) is False
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_collection_sources "
                   "WHERE collection_id = ? AND owner_user_id = ?",
                   (a["collection"], OWNER_B)) == 0


def test_b_cannot_add_a_source_into_its_own_collection(store, manager, a, b):
    assert store.collections.add_source(OWNER_B, b["collection"], a["source"]) is False
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_collection_sources "
                   "WHERE source_id = ? AND owner_user_id = ?",
                   (a["source"], OWNER_B)) == 0


def test_b_cannot_remove_a_source_from_a_collection(store, manager, a):
    assert store.collections.add_source(OWNER_A, a["collection"], a["source"]) is True
    assert store.collections.remove_source(OWNER_B, a["collection"], a["source"]) is False
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_collection_sources "
                   "WHERE collection_id = ? AND source_id = ?",
                   (a["collection"], a["source"])) == 1


def test_remove_missing_link_reports_false(store, a):
    assert store.collections.remove_source(OWNER_A, a["collection"], a["source"]) is False


def test_names_for_source_are_owner_scoped(store, a, b):
    assert store.collections.add_source(OWNER_A, a["collection"], a["source"]) is True
    assert store.collections.names_for_source(OWNER_A, a["source"]) == ["alpha collection"]
    assert store.collections.names_for_source(OWNER_B, a["source"]) == []


def test_same_owner_collection_membership_succeeds(store, manager, a):
    assert store.collections.add_source(OWNER_A, a["collection"], a["source"]) is True
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_collection_sources "
                   "WHERE collection_id = ? AND owner_user_id = ?",
                   (a["collection"], OWNER_A)) == 1


# --- Storage verification --------------------------------------------------

def _register_a(store, source_id: int, key: str) -> bool:
    return store.storage.register(
        owner_user_id=OWNER_A, source_id=source_id, storage_key=key,
        file_name="a.txt", file_format="txt", size_bytes=10, checksum="sum")


def test_b_cannot_register_storage_for_a_source(store, manager, a, b):
    assert store.storage.register(
        owner_user_id=OWNER_B, source_id=a["source"], storage_key="key/b-steal",
        file_name="x.txt", file_format="txt", size_bytes=1, checksum="s") is False
    assert _scalar(manager, "SELECT COUNT(*) FROM knowledge_storage_files "
                   "WHERE storage_key = ?", ("key/b-steal",)) == 0


def test_b_cannot_mark_or_read_a_sources_verification(store, manager, a):
    assert _register_a(store, a["source"], "key/alpha-file") is True
    assert store.storage.mark_verified(OWNER_B, a["source"]) is False
    assert store.storage.verified(OWNER_B, a["source"]) is False
    assert _scalar(manager, "SELECT verified FROM knowledge_storage_files "
                   "WHERE storage_key = ?", ("key/alpha-file",)) in (0, False)


def test_owner_can_mark_and_read_verification(store, manager, a):
    assert _register_a(store, a["source"], "key/alpha-verify") is True
    assert store.storage.verified(OWNER_A, a["source"]) is False
    assert store.storage.mark_verified(OWNER_A, a["source"]) is True
    assert store.storage.verified(OWNER_A, a["source"]) is True


def test_mark_verified_without_storage_row_reports_false(store, b):
    assert store.storage.mark_verified(OWNER_B, b["source"]) is False
    assert store.storage.verified(OWNER_B, b["source"]) is False
