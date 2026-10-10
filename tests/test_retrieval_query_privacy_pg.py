"""PostgreSQL companion: retrieval events never store query text.

Runs the repository-level policy against a real PostgreSQL 16 server. The
end-to-end retrieval path (ingestion + search) is covered on SQLite in
tests/test_retrieval_query_privacy.py; it is not re-run here.
Skipped automatically when PostgreSQL is unreachable.
"""
import test_repositories_pg as _pg

globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})

SECRET = "zq-secret-medical-question-7731"


def _rows(manager, sql, params=()):
    with manager._lock:
        cursor = manager._get_connection().cursor()
        cursor.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


def test_log_stores_no_query_text_on_postgres(pg_db):
    from app.knowledge.repositories import KnowledgeStore

    store = KnowledgeStore(pg_db._manager)
    event_id = store.events.log(owner_user_id=1, top_k=3, result_count=2,
                                latency_ms=1.0, generator="local")
    assert isinstance(event_id, int) and event_id > 0
    assert _rows(pg_db._manager,
                 "SELECT query, result_count FROM knowledge_retrieval_events "
                 "WHERE event_id = ?", (event_id,)) == [("", 2)]


def test_legacy_scrub_blanks_text_only_on_postgres(pg_db):
    from app.knowledge.repositories import KnowledgeStore

    manager = pg_db._manager
    store = KnowledgeStore(manager)
    _rows(manager,
          "INSERT INTO knowledge_retrieval_events "
          "(owner_user_id, query, top_k, result_count, latency_ms, generator) "
          "VALUES (?, ?, ?, ?, ?, ?) RETURNING event_id",
          (1, SECRET, 3, 0, 1.0, "local"))
    total = _rows(manager, "SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0]

    assert store.events.scrub_legacy_query_text() >= 1
    assert _rows(manager, "SELECT COUNT(*) FROM knowledge_retrieval_events "
                          "WHERE query <> ''")[0][0] == 0
    assert _rows(manager, "SELECT COUNT(*) FROM knowledge_retrieval_events")[0][0] == total
    assert store.events.scrub_legacy_query_text() == 0
