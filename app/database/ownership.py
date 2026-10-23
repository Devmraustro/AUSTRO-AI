"""Batched owner checks for cross-table references.

A row that references a parent row (for example a learning session pointing at
an objective, or a citation pointing at a chunk) must not reference a parent
owned by a different user, even when the child row's own ``owner_user_id`` is
correct. Repositories call :func:`refs_owned` inside their write lock, on the
same cursor, before the INSERT/UPDATE. A failed check means the caller returns
its failure value and writes nothing.

Guarantee and limits: the check and the write run in one lock-held transaction
in this process, so they cannot interleave with another write from this
process. A concurrent delete of the parent by another process can still race
with the insert (no hard FK exists for these owner-scoped references, because a
SQLite FK rebuild would be required). This is documented in
LEARNING_ARCHITECTURE.md section 11a and KNOWLEDGE_ARCHITECTURE.md section 9.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Tuple

# Every (table, column) pair that a repository may check. Table and column names
# are interpolated into SQL, so they must come from this fixed allow-list and
# never from caller input.
OWNED_REFERENCE_TARGETS = frozenset({
    # learning
    ("learning_goals", "goal_id"),
    ("learning_objectives", "objective_id"),
    ("learning_curricula", "curriculum_id"),
    ("learning_lessons", "lesson_id"),
    ("learning_sessions", "session_id"),
    # knowledge
    ("knowledge_sources", "source_id"),
    ("knowledge_collections", "collection_id"),
    ("knowledge_chunks", "chunk_id"),
    ("knowledge_retrieval_events", "event_id"),
})


def _as_ids(ids: Iterable[Any]) -> Optional[set]:
    try:
        return {int(i) for i in ids if i is not None}
    except (TypeError, ValueError):
        return None


def refs_owned(cursor, owner_user_id: int,
               refs: List[Tuple[str, str, Iterable[Any]]]) -> bool:
    """Return True only if every non-null referenced ID belongs to the owner.

    ``refs`` is a list of ``(table, column, ids)``. Each table costs exactly one
    batched ``COUNT(DISTINCT ...)`` query. A missing, foreign, or malformed ID
    makes the whole check fail. An empty or all-null list passes.
    """
    for table, column, ids in refs:
        if (table, column) not in OWNED_REFERENCE_TARGETS:
            raise ValueError(f"unsupported ownership reference: {table}.{column}")
        wanted = _as_ids(ids)
        if wanted is None:
            return False
        if not wanted:
            continue
        marks = ", ".join("?" for _ in wanted)
        # Table/column come from OWNED_REFERENCE_TARGETS (checked above); only
        # the IN-list placeholders vary, and every value is bound.
        cursor.execute(
            f"SELECT COUNT(DISTINCT {column}) FROM {table} "  # nosec B608
            f"WHERE owner_user_id = ? AND {column} IN ({marks})",
            (owner_user_id, *sorted(wanted)),
        )
        row = cursor.fetchone()
        if int(row[0]) != len(wanted):
            return False
    return True


def parent_owned(cursor, owner_user_id: int, table: str, column: str,
                 value: Any) -> bool:
    """Single mandatory parent: False when missing (None) or not owned."""
    if value is None:
        return False
    return refs_owned(cursor, owner_user_id, [(table, column, [value])])


# Fixed SQL, no interpolation. Each check is a single parameterized query.

_DOCUMENT_MATCHES_SOURCE_SQL = (
    "SELECT COUNT(*) FROM knowledge_documents d "
    "JOIN knowledge_sources s ON s.source_id = d.source_id "
    "AND s.owner_user_id = d.owner_user_id "
    "WHERE d.document_id = ? AND d.owner_user_id = ? AND d.source_id = ?"
)

_SECTION_MATCHES_SQL = (
    "SELECT COUNT(*) FROM knowledge_sections "
    "WHERE section_id = ? AND owner_user_id = ? AND document_id = ? AND source_id = ?"
)


def document_matches_source(cursor, owner_user_id: int, document_id: Any,
                            source_id: Any) -> bool:
    """True only if the document is owned by the owner, its stored source_id
    equals ``source_id``, and that source is also owned by the same owner."""
    if document_id is None or source_id is None:
        return False
    cursor.execute(_DOCUMENT_MATCHES_SOURCE_SQL,
                   (document_id, owner_user_id, source_id))
    return int(cursor.fetchone()[0]) == 1


def section_matches(cursor, owner_user_id: int, section_id: Any,
                    document_id: Any, source_id: Any) -> bool:
    """True only if the section is owned by the owner and its stored document_id
    and source_id both equal the supplied values."""
    if section_id is None or document_id is None or source_id is None:
        return False
    cursor.execute(_SECTION_MATCHES_SQL,
                   (section_id, owner_user_id, document_id, source_id))
    return int(cursor.fetchone()[0]) == 1
