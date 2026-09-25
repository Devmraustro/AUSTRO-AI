"""
AUSTRO AI - PostgreSQL dialect translation (schema DDL + repository queries).

The schema, migration and repository SQL in this codebase is authored once in a
SQLite-flavoured form that is also PostgreSQL-compatible after a small,
deterministic set of textual transforms. This module performs those transforms.

Two layers:
1. DDL translation - `to_postgresql()` for schema/migration CREATE statements.
2. Query translation - `translate_query()` plus `PolyglotCursor`, the cursor
   subclass used for every PostgreSQL connection. It transparently adapts the
   SQLite-native repository SQL to PostgreSQL while keeping the repository layer
   itself engine-agnostic:

   Supported query transforms:
   - '?' bind placeholder            ->  '%s' (psycopg2 paramstyle)
   - INSERT OR REPLACE INTO <t> ...  ->  PostgreSQL upsert (ON CONFLICT _ DO
                                          UPDATE SET col = EXCLUDED.col) for the
                                          tables registered in
                                          _REPLACE_TABLE_CONFLICT.
   - INSERT OR IGNORE INTO <t> ...   ->  INSERT ... ON CONFLICT DO NOTHING
   - scalar MAX(a, b)                ->  GREATEST(a, b)  (SQLite-only aggregate
                                          form; e.g. longest_streak rule)
   - INSERT INTO <auto-id table> ... ->  INSERT ... RETURNING <pk>
   It also emulates `cursor.lastrowid` (not available in psycopg2) and
   normalizes result rows so PostgreSQL values match what SQLite returns to the
   application (datetime/date -> TEXT, Decimal -> float, bool -> 0/1), via the
   int-indexable `CompatRow` (supports row["col"] AND row[0], like sqlite3.Row).

   No SQL round-trips ever reach PostgreSQL untranslated through this cursor.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date as _date_cls
from datetime import datetime as _datetime_cls
from datetime import time as _time_cls
from decimal import Decimal
from typing import Dict, List, Optional, Pattern, Tuple

_INTEGER_PK_AUTOINCREMENT_RE: Pattern = re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)
_BOOLEAN_DEFAULT_1_RE: Pattern = re.compile(r"\bBOOLEAN\s+DEFAULT\s+1\b", re.IGNORECASE)
_BOOLEAN_DEFAULT_0_RE: Pattern = re.compile(r"\bBOOLEAN\s+DEFAULT\s+0\b", re.IGNORECASE)
_QMARK_RE: Pattern = re.compile(r"\?")

# --- Repository-query translation rules -------------------------------------

# Tables whose auto-generated PK is read back via INSERT ... RETURNING on PG.
# (These are exactly the INSERT sites in the repository layer that use
# `cursor.lastrowid` with an INTEGER PRIMARY KEY AUTOINCREMENT / SERIAL column.)
_AUTOID_PK: Dict[str, str] = {
    "goals": "goal_id",
    "habits": "habit_id",
    "reminders": "reminder_id",
    "knowledge_sources": "source_id",
    "knowledge_documents": "document_id",
    "knowledge_sections": "section_id",
    "knowledge_chunks": "chunk_id",
    "knowledge_retrieval_events": "event_id",
    "knowledge_collections": "collection_id",
    "memories": "memory_id",
    "memory_versions": "version_id",
    "memory_events": "event_id",
    "learning_goals": "goal_id",
    "learning_objectives": "objective_id",
    "learning_curricula": "curriculum_id",
    "learning_lessons": "lesson_id",
    "learning_sessions": "session_id",
    "learning_assessments": "attempt_id",
}

# Tables using `INSERT OR REPLACE` whose unique key enables a PG upsert.
# The REPLACE conflict resolution targets the same unique constraints that SQLite
# uses for its REPLACE (table primary key / declared UNIQUE indexes).
_REPLACE_TABLE_CONFLICT: Dict[str, List[str]] = {
    "users": ["user_id"],
    "knowledge_embeddings": ["chunk_row_id", "model", "version"],
    "learning_plans": ["owner_user_id", "plan_date"],
    "learning_progress": ["owner_user_id", "record_date"],
}

# `INSERT OR REPLACE` on a table with NO unique constraint in this schema is a
# plain INSERT in SQLite (REPLACE can never conflict). Preserve that exact
# behaviour on PostgreSQL by stripping the OR REPLACE modifier only.
_PLAIN_INSERT_REPLACE_TABLES: Tuple[str, ...] = ("daily_reviews",)

# Tables using `INSERT OR IGNORE` -> ON CONFLICT DO NOTHING (no conflict target
# required for DO NOTHING on either engine).
_IGNORE_TABLES: Tuple[str, ...] = ("knowledge_collection_sources", "learning_reviews")

_INSERT_TABLE_RE: Pattern = re.compile(
    r"^\s*INSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)
_OR_REPLACE_RE: Pattern = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO", re.IGNORECASE)
_OR_IGNORE_RE: Pattern = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_MAX2_RE: Pattern = re.compile(r"MAX\(\s*longest_streak\s*,\s*current_streak\s*\+\s*1\s*\)", re.IGNORECASE)
_COLUMN_SPLIT_RE: Pattern = re.compile(r"\s*,\s*")


def to_postgresql(ddl: str) -> str:
    """Translate a single SQLite-flavoured DDL/statement to PostgreSQL."""
    out = _INTEGER_PK_AUTOINCREMENT_RE.sub("SERIAL PRIMARY KEY", ddl)
    out = _BOOLEAN_DEFAULT_1_RE.sub("BOOLEAN DEFAULT TRUE", out)
    out = _BOOLEAN_DEFAULT_0_RE.sub("BOOLEAN DEFAULT FALSE", out)
    return out


def to_postgresql_params(statement: str) -> str:
    """Translate a parameterised statement to PostgreSQL's %s bind style."""
    return _QMARK_RE.sub("%s", to_postgresql(statement))


def upsert_schema_migrations(pk_column: str = "schema_name") -> str:
    """PostgreSQL upsert used to record an applied migration idempotently."""
    return (
        f"INSERT INTO schema_migrations ({pk_column}, version) VALUES (%s, %s) "
        f"ON CONFLICT ({pk_column}) DO UPDATE SET version = EXCLUDED.version"
    )


def translate_statements(statements: List[str]) -> List[str]:
    """Translate a list of DDL statements to PostgreSQL DDL."""
    return [to_postgresql(statement) for statement in statements]


def _split_columns(column_list: str) -> List[str]:
    """Split a raw column-list fragment into trimmed column names."""
    raw = _COLUMN_SPLIT_RE.split(column_list.strip())
    return [c.strip().strip('"') for c in raw if c.strip()]


def _parse_table(sql: str) -> Optional[str]:
    """Return the (lower-cased) INSERT target table name, if any."""
    match = _INSERT_TABLE_RE.match(sql)
    return match.group(1).lower() if match else None


def _parse_insert_columns(sql: str) -> Optional[List[str]]:
    """Return the explicit INSERT column list for a matched statement."""
    match = _INSERT_TABLE_RE.match(sql)
    if not match:
        return None
    columns = match.group(2)
    if not columns or not columns.strip():
        return None
    return _split_columns(columns)


def translate_query(sql: str) -> Tuple[str, bool]:
    """
    Translate one SQLite-native repository statement for PostgreSQL.

    Returns ``(translated_sql, returns_primary_key)`` where ``returns_primary_key``
    is True when an ``INSERT ... RETURNING <pk>`` clause was appended (so the
    cursor can feed ``cursor.lastrowid``).
    """
    out = _QMARK_RE.sub("%s", sql)
    out = _MAX2_RE.sub("GREATEST(longest_streak, current_streak + 1)", out)

    table = _parse_table(out)
    returns_primary_key = False

    if table is None:
        return out, returns_primary_key

    if _OR_REPLACE_RE.match(out) or _OR_IGNORE_RE.match(out):
        if _OR_IGNORE_RE.match(out):
            out = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", out, count=1, flags=re.IGNORECASE)
            out = f"{out} ON CONFLICT DO NOTHING"
            # Recompute the target table after the modifier was stripped; keep an
            # explicit table read for RETURNING decisions below.
            table = _parse_table(out)
        elif table in _REPLACE_TABLE_CONFLICT:
            columns = _parse_insert_columns(out) or []
            conflict = _REPLACE_TABLE_CONFLICT[table]
            update_cols = [c for c in columns if c not in conflict]
            out = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", out, count=1, flags=re.IGNORECASE)
            if update_cols:
                assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                out = (
                    f"{out} ON CONFLICT ({', '.join(conflict)}) "
                    f"DO UPDATE SET {assignments}"
                )
            else:
                # No non-conflict columns (defensive; conflicts cannot be
                # "updated") - fall back to DO NOTHING semantics.
                out = f"{out} ON CONFLICT DO NOTHING"
        elif table in _PLAIN_INSERT_REPLACE_TABLES:
            out = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", out, count=1, flags=re.IGNORECASE)
        else:
            # Unknown OR REPLACE table: degrade to a plain INSERT on PG so the
            # statement never becomes a syntax error.
            out = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", out, count=1, flags=re.IGNORECASE)

    if (
        table in _AUTOID_PK
        and " RETURNING " not in out.upper()
        and "ON CONFLICT" not in out.upper()
    ):
        out = f"{out} RETURNING {_AUTOID_PK[table]}"
        returns_primary_key = True

    return out, returns_primary_key


def normalize_value(value):
    """Coerce a PostgreSQL result value to the form SQLite returns to the app."""
    if isinstance(value, _datetime_cls):
        return value.isoformat(sep=" ")
    if isinstance(value, _date_cls):
        return value.isoformat()
    if isinstance(value, _time_cls):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return int(value)
    return value


class CompatRow(dict):
    """
    Row type for PostgreSQL results.

    Supports both ``row["column"]`` and ``row[0]`` and ``dict(row)``, mirroring
    the behaviour of ``sqlite3.Row`` so the repository layer is engine-agnostic.
    Values are normalized to the representations SQLite returns (see
    ``normalize_value``).
    """

    __slots__ = ("_order",)

    def __init__(self, keys, values):
        super().__init__(zip(keys, values))
        self._order = tuple(keys)

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)


# --- PostgreSQL cursor -------------------------------------------------------

try:  # pragma: no cover - import guard for SQLite-only environments
    import psycopg2
    from psycopg2.extras import DictCursor
except Exception:  # pragma: no cover  # noqa: BLE001
    psycopg2 = None
    DictCursor = None

DB_ERROR: tuple = (sqlite3.Error,) if psycopg2 is None else (sqlite3.Error, psycopg2.Error)

if DictCursor is not None:  # pragma: no cover - import guard

    class PolyglotCursor(DictCursor):
        """
        PostgreSQL cursor used for every repository connection.

        Translates SQLite-native repository SQL to PostgreSQL (see
        ``translate_query``), emulates ``cursor.lastrowid`` and returns
        ``CompatRow`` rows with SQLite-equivalent value types.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._austro_lastrowid = None

        # -- SQL translation + id retrieval -----------------------------------

        def execute(self, sql, vars=None):
            pg_sql, returns_pk = translate_query(sql) if isinstance(sql, str) else (sql, False)
            super().execute(pg_sql, vars)
            if returns_pk:
                row = super().fetchone()
                self._austro_lastrowid = row[0] if row is not None else None
            else:
                self._austro_lastrowid = None

        def executemany(self, sql, seq_of_parameters):
            pg_sql, returns_pk = translate_query(sql) if isinstance(sql, str) else (sql, False)
            super().executemany(pg_sql, seq_of_parameters)
            if returns_pk:
                rows = super().fetchall()
                self._austro_lastrowid = rows[-1][0] if rows else None
            else:
                self._austro_lastrowid = None

        @property
        def lastrowid(self):
            return self._austro_lastrowid

        # -- Row materialization ----------------------------------------------

        @staticmethod
        def _row(record):
            if record is None:
                return None
            if isinstance(record, CompatRow):
                return record
            keys = list(record.keys())
            return CompatRow(keys, [normalize_value(record[k]) for k in keys])

        def fetchone(self):
            return self._row(super().fetchone())

        def fetchall(self):
            return [self._row(r) for r in super().fetchall()]

        def fetchmany(self, size=None):
            if size is None:
                rows = super().fetchmany()
            else:
                rows = super().fetchmany(size)
            return [self._row(r) for r in rows]

else:  # pragma: no cover - import guard

    class PolyglotCursor:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("psycopg2 is not installed")


def get_postgresql_cursor_factory():
    """Return the PostgreSQL cursor class (PolyglotCursor) or None if unavailable."""
    if DictCursor is None:
        return None
    return PolyglotCursor