"""
AUSTRO AI - Seed realistic rows into the PostgreSQL instance for the backup/
restore drill (idempotent: deletes its tagged rows first via TRUNCATE...JSON).
"""
from __future__ import annotations

import os
import sys

os.environ["DB_ENGINE"] = "postgresql"

import psycopg2  # noqa: E402


def main() -> int:
    import psycopg2 as pg2

    conn = pg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "austro_ai"),
        user=os.environ.get("DB_USER", "austro"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        # delete prior drill rows
        cur.execute("DELETE FROM knowledge_embeddings")
        cur.execute("DELETE FROM knowledge_chunks")
        cur.execute("DELETE FROM knowledge_sections")
        cur.execute("DELETE FROM knowledge_documents")
        cur.execute("DELETE FROM knowledge_sources")
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM goals")
        cur.execute("DELETE FROM users")

        cur.execute(
            "INSERT INTO users (user_id, username, first_name, education_level, current_mode) "
            "VALUES (1001, 'drill_user_a', 'Drill A', 'high_school', 'study'), "
            "(1002, 'drill_user_b', 'Drill B', 'university', 'general')"
        )
        cur.execute(
            "INSERT INTO goals (user_id, title, category, status) VALUES "
            "(1001, 'اللغة الإنجليزية', 'language', 'active'), "
            "(1002, 'البرمجة', 'tech', 'active'), "
            "(1002, 'الرياضيات', 'science', 'active')"
        )
        cur.execute(
            "INSERT INTO memories (owner_user_id, scope, memory_type, subject, claim, "
            "provenance, importance, hash_key) VALUES "
            "(1001, 'USER', 'preference', 'study', 'يفضل التعلم صباحاً', 'drill', 3, 'drill_h1'), "
            "(1001, 'USER', 'fact', 'breakfast', 'لا يحب القهوة', 'drill', 2, 'drill_h2')"
        )
        cur.execute(
            "INSERT INTO knowledge_sources (source_id, owner_user_id, source_type, title, "
            "file_format, status, checksum) VALUES "
            "(9001, 1001, 'pdf', 'بيولوجيا', 'pdf', 'ready', 'aa11bb22cc33dd44ee55ff6677889900')"
        )
        cur.execute(
            "INSERT INTO knowledge_documents (document_id, source_id, owner_user_id, title, "
            "total_chars, total_pages, status) VALUES "
            "(9001, 9001, 1001, 'بيولوجيا', 12000, 40, 'ready')"
        )
        cur.execute(
            "INSERT INTO knowledge_sections (section_id, document_id, owner_user_id, source_id, "
            "level, title, order_index) VALUES "
            "(9001, 9001, 1001, 9001, 1, 'الفصل الأول', 1)"
        )
        cur.execute(
            "INSERT INTO knowledge_chunks (chunk_id, owner_user_id, source_id, document_id, "
            "section_id, chunk_key, content, content_hash, order_index) VALUES "
            "(9001, 1001, 9001, 9001, 9001, '9001:1', 'الخلية هي وحدة الكائن الحي', 'c1', 1), "
            "(9002, 1001, 9001, 9001, 9001, '9001:2', 'الوراثة علم نقل الصفات', 'c2', 2)"
        )
        cur.execute(
            "INSERT INTO knowledge_embeddings (embedding_id, owner_user_id, source_id, "
            "chunk_row_id, model, version, dimensions, vector_json) VALUES "
            "(9001, 1001, 9001, 9001, 'local-hash', '1', 128, '[0.1,0.2]'), "
            "(9002, 1001, 9001, 9002, 'local-hash', '1', 128, '[0.3,0.4]')"
        )
        cur.execute(
            "INSERT INTO learning_goals (goal_id, owner_user_id, kind, title, status) VALUES "
            "(8001, 1001, 'short_term', 'فهم الخلية', 'active')"
        )
        cur.execute(
            "INSERT INTO learning_objectives (objective_id, owner_user_id, goal_id, title, status, "
            "mastery_state, mastery_score) VALUES "
            "(8001, 1001, 8001, 'تعريف الخلية', 'planned', 'NEW', 0), "
            "(8002, 1001, 8001, 'أغشية الخلية', 'planned', 'NEW', 0)"
        )
        # summary
        cur.execute("SELECT count(*) FROM users")
        users = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM knowledge_chunks")
        chunks = cur.fetchone()[0]
    conn.close()
    print(f"Seeded: users={users}, knowledge_chunks={chunks}, sources, memories, goals")
    return 0


if __name__ == "__main__":
    sys.exit(main())