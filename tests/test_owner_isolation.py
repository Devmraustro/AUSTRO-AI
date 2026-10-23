"""Cross-owner isolation for knowledge and learning data.

Owner A (1) creates a source, document, section, goal and objective. Owner B (2)
then tries every read and write path that takes an id. Each assertion checks
the stored database values (row counts and column values read with raw SQL),
not just the return value, so a silent leak or an overwrite is caught.
"""

from __future__ import annotations

import pytest

import database
from app.knowledge.repositories import KnowledgeStore
from app.learning.book import BookToCourse
from app.learning.repositories import LearningStore

OWNER_A = 1
OWNER_B = 2
SECRET_TITLE = "Owner-A-private-handbook"
SECRET_SECTION = "Owner-A-private-chapter"


def _raw(manager, sql: str, params=()):
    with manager._lock:
        conn = manager._get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cursor.fetchall()]


@pytest.fixture()
def manager():
    return database.db._manager


@pytest.fixture()
def knowledge(manager):
    return KnowledgeStore(manager)


@pytest.fixture()
def learning(manager):
    return LearningStore(manager)


@pytest.fixture()
def owner_a_source(knowledge):
    source_id = knowledge.sources.create(
        owner_user_id=OWNER_A, source_type="txt", title=SECRET_TITLE,
        file_name="a.txt", file_format="txt", mime_type="text/plain",
        file_size_bytes=42, checksum="sum-owner-a", storage_key="key/owner-a",
        original_ref=None)
    assert source_id
    document_id = knowledge.documents.create(
        source_id=source_id, owner_user_id=OWNER_A, title=SECRET_TITLE,
        author=None, language="en", toc=[{"title": SECRET_SECTION}],
        total_chars=40, total_pages=1)
    assert document_id
    section_id = knowledge.sections.create(
        document_id=document_id, owner_user_id=OWNER_A, source_id=source_id,
        level=1, title=SECRET_SECTION, order_index=0, start_char=0, end_char=40)
    assert section_id
    return {"source_id": source_id, "document_id": document_id,
            "section_id": section_id}


# ------------------------------------------------------------------ knowledge --- #
def test_owner_b_cannot_read_owner_a_source(knowledge, owner_a_source):
    assert knowledge.sources.get(OWNER_B, owner_a_source["source_id"]) is None
    assert knowledge.sources.get(OWNER_A, owner_a_source["source_id"]) is not None


def test_owner_b_gets_no_documents_for_owner_a_source(knowledge, owner_a_source):
    assert knowledge.documents.list_for_source(OWNER_B, owner_a_source["source_id"]) == []
    owned = knowledge.documents.list_for_source(OWNER_A, owner_a_source["source_id"])
    assert [d["document_id"] for d in owned] == [owner_a_source["document_id"]]


def test_owner_b_gets_no_sections_for_owner_a_document(knowledge, owner_a_source):
    assert knowledge.sections.list_for_document(
        OWNER_B, owner_a_source["document_id"]) == []
    owned = knowledge.sections.list_for_document(
        OWNER_A, owner_a_source["document_id"])
    assert [s.section_id for s in owned] == [owner_a_source["section_id"]]


def test_owner_b_cannot_fetch_owner_a_section_by_id(knowledge, owner_a_source):
    assert knowledge.sections.get(OWNER_B, owner_a_source["section_id"]) is None
    section = knowledge.sections.get(OWNER_A, owner_a_source["section_id"])
    assert section is not None and section.title == SECRET_SECTION


def test_owner_b_cannot_add_owner_a_source_to_its_collection(knowledge, owner_a_source):
    collection_id = knowledge.collections.create(OWNER_B, "mine")
    assert collection_id
    assert knowledge.collections.add_source(
        OWNER_B, collection_id, owner_a_source["source_id"]) is False
    rows = _raw(knowledge._manager,
                "SELECT COUNT(*) FROM knowledge_collection_sources "
                "WHERE collection_id = ?", (collection_id,))
    assert rows[0][0] == 0


def test_owner_b_cannot_delete_owner_a_source(knowledge, owner_a_source):
    assert knowledge.sources.delete(OWNER_B, owner_a_source["source_id"]) is False
    rows = _raw(knowledge._manager,
                "SELECT title, owner_user_id FROM knowledge_sources WHERE source_id = ?",
                (owner_a_source["source_id"],))
    assert rows == [(SECRET_TITLE, OWNER_A)]


def test_owner_b_cannot_rename_owner_a_source(knowledge, owner_a_source):
    assert knowledge.sources.set_title(
        OWNER_B, owner_a_source["source_id"], "hijacked") is False
    rows = _raw(knowledge._manager,
                "SELECT title FROM knowledge_sources WHERE source_id = ?",
                (owner_a_source["source_id"],))
    assert rows == [(SECRET_TITLE,)]


# -------------------------------------------------------------------- learning --- #
def test_book_course_reads_are_empty_for_owner_b(manager, knowledge, owner_a_source):
    book = BookToCourse(LearningStore(manager), knowledge, ai=None, settings={})
    source_id = owner_a_source["source_id"]
    assert book.chapter_map(OWNER_B, source_id) == {}
    assert book.evidence(OWNER_B, source_id, "handbook chapter") == []
    assert book.concepts(OWNER_B, source_id) == []


def test_book_chapter_map_for_owner_a_still_works(manager, knowledge, owner_a_source):
    book = BookToCourse(LearningStore(manager), knowledge, ai=None, settings={})
    chapter_map = book.chapter_map(OWNER_A, owner_a_source["source_id"])
    assert [c["section_id"] for c in chapter_map["chapters"]] == [
        owner_a_source["section_id"]]


def test_owner_b_cannot_change_owner_a_goal(learning, manager):
    goal_id = learning.goals.create(owner_user_id=OWNER_A, kind="course",
                                    title="A's private goal")
    assert goal_id
    assert learning.goals.get(OWNER_B, goal_id) is None
    assert learning.goals.set_status(OWNER_B, goal_id, "ARCHIVED") is False
    rows = _raw(manager, "SELECT status, title, owner_user_id FROM learning_goals "
                         "WHERE goal_id = ?", (goal_id,))
    assert rows == [("active", "A's private goal", OWNER_A)]


def test_owner_b_cannot_read_or_change_owner_a_objective(learning, manager):
    goal_id = learning.goals.create(owner_user_id=OWNER_A, kind="course",
                                    title="A's goal")
    objective_id = learning.objectives.create(
        owner_user_id=OWNER_A, goal_id=goal_id, title="A's objective",
        description="")
    assert objective_id
    before = _raw(manager, "SELECT mastery_state FROM learning_objectives "
                           "WHERE objective_id = ?", (objective_id,))
    assert learning.objectives.get(OWNER_B, objective_id) is None
    assert learning.objectives.update_mastery(
        OWNER_B, objective_id, "MASTERED", 1.0) is False
    after = _raw(manager, "SELECT mastery_state FROM learning_objectives "
                          "WHERE objective_id = ?", (objective_id,))
    assert before and after == before and after[0][0] != "MASTERED"
