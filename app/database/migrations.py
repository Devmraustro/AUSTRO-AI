"""
AUSTRO AI - Schema migrations.

Each migration carries a schema name + version and a list of DDL statements.
`apply_migrations()` runs any migration whose (schema_name, version) is not yet
recorded in `schema_migrations`, so existing databases are upgraded in place
and fresh databases get everything. Never drops or rewrites existing data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from app.config.settings import settings
from app.database.dialect import DB_ERROR, translate_statements, upsert_schema_migrations

logger = logging.getLogger(__name__)

_DB_ENGINE = settings.db_engine

_MIGRATIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        schema_name TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


@dataclass(frozen=True)
class Migration:
    schema_name: str
    version: int
    statements: List[str] = field(default_factory=list)


# Knowledge engine schema (books, documents, chunks, embeddings, evidence).
KNOWLEDGE_V1 = Migration(
    schema_name="knowledge",
    version=1,
    statements=[
        """
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'book',
            title TEXT NOT NULL,
            author TEXT,
            file_name TEXT,
            file_format TEXT,
            mime_type TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            checksum TEXT,
            storage_key TEXT,
            original_ref TEXT,
            language TEXT,
            pages INTEGER DEFAULT 0,
            char_count INTEGER DEFAULT 0,
            metadata_json TEXT,
            status TEXT DEFAULT 'pending',
            ingestion_state TEXT DEFAULT 'UPLOAD',
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, checksum)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_sources_owner "
        "ON knowledge_sources(owner_user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_knowledge_sources_status "
        "ON knowledge_sources(status)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(source_id)
                ON DELETE CASCADE,
            UNIQUE (source_id, checksum)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_collections (
            collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_collection_sources (
            collection_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (collection_id, source_id),
            FOREIGN KEY (collection_id) REFERENCES knowledge_collections(collection_id)
                ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(source_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            document_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            author TEXT,
            language TEXT,
            toc_json TEXT,
            total_chars INTEGER DEFAULT 0,
            total_pages INTEGER DEFAULT 0,
            total_sections INTEGER DEFAULT 0,
            status TEXT DEFAULT 'processing',
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(source_id)
                ON DELETE CASCADE,
            UNIQUE (source_id, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_sections (
            section_id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            level INTEGER DEFAULT 0,
            title TEXT NOT NULL,
            order_index INTEGER DEFAULT 0,
            start_char INTEGER,
            end_char INTEGER,
            parent_section_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_sections_doc "
        "ON knowledge_sections(document_id, order_index)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            document_id INTEGER NOT NULL,
            section_id INTEGER,
            chunk_key TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            token_count INTEGER DEFAULT 0,
            char_count INTEGER DEFAULT 0,
            page TEXT,
            order_index INTEGER DEFAULT 0,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(source_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id)
                ON DELETE CASCADE,
            FOREIGN KEY (section_id) REFERENCES knowledge_sections(section_id)
                ON DELETE SET NULL,
            UNIQUE (owner_user_id, chunk_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source "
        "ON knowledge_chunks(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_owner "
        "ON knowledge_chunks(owner_user_id)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_embeddings (
            embedding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            chunk_row_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            version TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chunk_row_id) REFERENCES knowledge_chunks(chunk_id)
                ON DELETE CASCADE,
            UNIQUE (chunk_row_id, model, version)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_owner_model "
        "ON knowledge_embeddings(owner_user_id, model, version)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_retrieval_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            top_k INTEGER,
            result_count INTEGER,
            latency_ms REAL,
            generator TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_citations (
            citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            chunk_row_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            title TEXT,
            section_title TEXT,
            page TEXT,
            snippet TEXT,
            score REAL,
            FOREIGN KEY (event_id) REFERENCES knowledge_retrieval_events(event_id)
                ON DELETE CASCADE,
            FOREIGN KEY (chunk_row_id) REFERENCES knowledge_chunks(chunk_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_storage_files (
            file_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            storage_key TEXT NOT NULL UNIQUE,
            file_name TEXT,
            file_format TEXT,
            size_bytes INTEGER,
            checksum TEXT,
            verified BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES knowledge_sources(source_id)
                ON DELETE CASCADE
        )
        """,
    ],
)

# Personal memory engine schema (typed memories, versions, audit trail).
MEMORY_V1 = Migration(
    schema_name="memory",
    version=1,
    statements=[
        """
        CREATE TABLE IF NOT EXISTS memory_prefs (
            owner_user_id INTEGER PRIMARY KEY,
            auto_memory_enabled INTEGER NOT NULL DEFAULT 1,
            consent_types TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memories (
            memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            scope TEXT NOT NULL DEFAULT 'USER',
            memory_type TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            claim TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'medium',
            importance INTEGER NOT NULL DEFAULT 3,
            provenance TEXT NOT NULL,
            source_note TEXT,
            consent_state TEXT NOT NULL DEFAULT 'automatic',
            decay_policy TEXT NOT NULL DEFAULT 'default',
            is_episodic INTEGER NOT NULL DEFAULT 0,
            event_date TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            hash_key TEXT NOT NULL,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            last_confirmed_at TIMESTAMP,
            UNIQUE (owner_user_id, hash_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_memories_owner "
        "ON memories(owner_user_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_owner_type "
        "ON memories(owner_user_id, memory_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_memories_scope "
        "ON memories(scope, memory_type)",
        """
        CREATE TABLE IF NOT EXISTS memory_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            claim TEXT NOT NULL,
            confidence TEXT NOT NULL,
            importance INTEGER,
            reason TEXT,
            actor INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(memory_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_memory_versions_memory "
        "ON memory_versions(memory_id, version)",
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            memory_id INTEGER,
            action TEXT NOT NULL,
            source TEXT,
            reason TEXT,
            actor INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(memory_id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_memory_events_owner "
        "ON memory_events(owner_user_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS memory_access_log (
            access_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            memory_id INTEGER,
            purpose TEXT NOT NULL DEFAULT 'context_pack',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(memory_id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_memory_access_owner_purpose "
        "ON memory_access_log(owner_user_id, purpose, created_at)",
    ],
)

# Adaptive learning engine schema (goals, objectives, curricula, lessons,
# sessions, mastery, spaced reviews, assessments, misconceptions, plans).
LEARNING_V1 = Migration(
    schema_name="learning",
    version=1,
    statements=[
        """
        CREATE TABLE IF NOT EXISTS learning_goals (
            goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'long_term',
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            parent_goal_id INTEGER,
            deadline TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_goal_id) REFERENCES learning_goals(goal_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_goals_owner "
        "ON learning_goals(owner_user_id, kind)",
        """
        CREATE TABLE IF NOT EXISTS learning_objectives (
            objective_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            goal_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            difficulty TEXT NOT NULL DEFAULT 'medium',
            prerequisites_json TEXT DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'planned',
            mastery_state TEXT NOT NULL DEFAULT 'NEW',
            mastery_score REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (goal_id) REFERENCES learning_goals(goal_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_objectives_goal "
        "ON learning_objectives(goal_id)",
        "CREATE INDEX IF NOT EXISTS idx_learning_objectives_owner "
        "ON learning_objectives(owner_user_id)",
        """
        CREATE TABLE IF NOT EXISTS learning_curricula (
            curriculum_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            goal_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'READ',
            modules_json TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (goal_id) REFERENCES learning_goals(goal_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_curricula_goal "
        "ON learning_curricula(goal_id)",
        """
        CREATE TABLE IF NOT EXISTS learning_lessons (
            lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            curriculum_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            source_id INTEGER,
            grounded INTEGER NOT NULL DEFAULT 0,
            content_json TEXT NOT NULL,
            sources_json TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE CASCADE,
            UNIQUE (owner_user_id, objective_id, curriculum_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_lessons_curriculum "
        "ON learning_lessons(curriculum_id)",
        """
        CREATE TABLE IF NOT EXISTS learning_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            goal_id INTEGER,
            objective_id INTEGER,
            curriculum_id INTEGER,
            mode TEXT DEFAULT 'READ',
            state TEXT NOT NULL DEFAULT 'STARTED',
            step TEXT NOT NULL DEFAULT 'LESSON',
            lesson_id INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            last_activity_at TIMESTAMP,
            result_json TEXT,
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE SET NULL,
            FOREIGN KEY (curriculum_id) REFERENCES learning_curricula(curriculum_id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_sessions_owner_state "
        "ON learning_sessions(owner_user_id, state)",
        """
        CREATE TABLE IF NOT EXISTS learning_mastery (
            owner_user_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'NEW',
            score REAL NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            recent_json TEXT DEFAULT '[]',
            kinds_json TEXT DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner_user_id, objective_id),
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS learning_reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            concept TEXT NOT NULL,
            difficulty TEXT NOT NULL DEFAULT 'medium',
            last_reviewed TEXT,
            next_review TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            interval_days INTEGER NOT NULL DEFAULT 1,
            ease REAL NOT NULL DEFAULT 2.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, objective_id, concept),
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_reviews_due "
        "ON learning_reviews(owner_user_id, next_review)",
        """
        CREATE TABLE IF NOT EXISTS learning_assessments (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            session_id INTEGER,
            objective_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            concept TEXT DEFAULT '',
            prompt TEXT NOT NULL,
            options_json TEXT DEFAULT '[]',
            expected TEXT DEFAULT '',
            keywords_json TEXT DEFAULT '[]',
            user_answer TEXT DEFAULT '',
            correct INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            feedback TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES learning_sessions(session_id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_assessments_owner "
        "ON learning_assessments(owner_user_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS learning_misconceptions (
            misconception_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            objective_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            evidence_json TEXT DEFAULT '[]',
            count INTEGER NOT NULL DEFAULT 1,
            severity TEXT NOT NULL DEFAULT 'medium',
            acknowledged INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, objective_id, pattern),
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_misconceptions_owner "
        "ON learning_misconceptions(owner_user_id, count)",
        """
        CREATE TABLE IF NOT EXISTS learning_plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            plan_date TEXT NOT NULL,
            items_json TEXT NOT NULL,
            source TEXT DEFAULT 'adaptive_planner',
            total_minutes INTEGER NOT NULL DEFAULT 0,
            adjusted INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, plan_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS learning_progress (
            progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            record_date TEXT NOT NULL,
            study_minutes INTEGER NOT NULL DEFAULT 0,
            practice_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            assessment_count INTEGER NOT NULL DEFAULT 0,
            assessment_score_avg REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_user_id, record_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS learning_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            objective_id INTEGER,
            session_id INTEGER,
            detail_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (objective_id) REFERENCES learning_objectives(objective_id)
                ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES learning_sessions(session_id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_learning_events_owner "
        "ON learning_events(owner_user_id, created_at)",
    ],
)

MIGRATIONS: List[Migration] = [KNOWLEDGE_V1, MEMORY_V1, LEARNING_V1]


def apply_migrations(conn) -> None:  # sqlite3.Connection or psycopg2 connection
    """Apply recorded migrations that are not yet present in this database.

    Engine-aware: PostgreSQL connections receive translated DDL, %s bind styles
    and an upsert for the schema_migrations bookkeeping row.
    """
    postgres = _DB_ENGINE == "postgresql"
    cursor = conn.cursor()
    cursor.execute(_MIGRATIONS_TABLE)
    for migration in MIGRATIONS:
        select_sql = (
            "SELECT version FROM schema_migrations WHERE schema_name = %s"
            if postgres
            else "SELECT version FROM schema_migrations WHERE schema_name = ?"
        )
        cursor.execute(select_sql, (migration.schema_name,))
        row = cursor.fetchone()
        if row is not None and len(row) > 0 and int(row[0]) >= migration.version:
            continue
        try:
            statements = (
                translate_statements(migration.statements) if postgres else migration.statements
            )
            for statement in statements:
                cursor.execute(statement)
            if postgres:
                cursor.execute(
                    upsert_schema_migrations(),
                    (migration.schema_name, migration.version),
                )
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO schema_migrations (schema_name, version) "
                    "VALUES (?, ?)",
                    (migration.schema_name, migration.version),
                )
            conn.commit()
            logger.info(
                f"Applied migration {migration.schema_name} v{migration.version}"
            )
        except DB_ERROR as exc:  # pragma: no cover - defensive
            conn.rollback()
            logger.exception(
                f"Migration {migration.schema_name} v{migration.version} failed: {exc}"
            )
            raise