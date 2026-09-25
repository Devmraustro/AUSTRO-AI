"""AUSTRO AI - Learning store (Phase E repositories).

Owner-scoped persistence for learning goals, objectives, curricula, lessons,
sessions, mastery, reviews, assessments, misconceptions, plans, progress and
the audit log. Mirrors the knowledge/memory repository pattern (thread-local
connections guarded by the manager lock; falsy-return on error).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.connection import DatabaseManager
from app.database.dialect import DB_ERROR

logger = logging.getLogger(__name__)

_JSON_EMPTY = "[]"
_OBJECT_EMPTY = "{}"


class _LearningBase:
    def __init__(self, manager: DatabaseManager):
        self._manager = manager

    def _connection(self) -> sqlite3.Connection:
        return self._manager._get_connection()

    def _now(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _j(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False) if value is not None else _JSON_EMPTY

    @staticmethod
    def _unjson(value: Optional[str], default: Any = None) -> Any:
        if not value:
            return default if default is not None else []
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default if default is not None else []


class EducationalGoalsRepository(_LearningBase):
    def create(self, *, owner_user_id: int, kind: str, title: str,
               description: str = "", parent_goal_id: Optional[int] = None,
               deadline: Optional[str] = None) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_goals "
                    "(owner_user_id, kind, title, description, parent_goal_id, "
                    "deadline) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner_user_id, kind, title, description, parent_goal_id,
                     deadline),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in learning goal create: {e}")
            return None

    def get(self, owner_user_id: int, goal_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_goals WHERE goal_id = ? "
                    "AND owner_user_id = ?",
                    (goal_id, owner_user_id),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except DB_ERROR as e:
            logger.error(f"Database error in learning goal get: {e}")
            return None

    def list(self, owner_user_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_goals WHERE owner_user_id = ? "
                    "ORDER BY kind, created_at",
                    (owner_user_id,),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in learning goal list: {e}")
            return []

    def children(self, owner_user_id: int, goal_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_goals WHERE owner_user_id = ? "
                    "AND parent_goal_id = ?",
                    (owner_user_id, goal_id),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in learning goal children: {e}")
            return []

    def set_status(self, owner_user_id: int, goal_id: int, status: str) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_goals SET status = ?, updated_at = ? "
                    "WHERE goal_id = ? AND owner_user_id = ?",
                    (status, self._now(), goal_id, owner_user_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in learning goal set_status: {e}")
            return False


class LearningObjectivesRepository(_LearningBase):
    def create(self, *, owner_user_id: int, goal_id: int, title: str,
               description: str = "", difficulty: str = "medium",
               prerequisites: Optional[List[int]] = None) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_objectives "
                    "(owner_user_id, goal_id, title, description, difficulty, "
                    "prerequisites_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner_user_id, goal_id, title, description, difficulty,
                     self._j(prerequisites or [])),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in objective create: {e}")
            return None

    def get(self, objective_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_objectives WHERE objective_id = ?",
                    (objective_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["prerequisites"] = self._unjson(data.pop("prerequisites_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in objective get: {e}")
            return None

    def list_for_goal(self, goal_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_objectives WHERE goal_id = ? "
                    "ORDER BY objective_id",
                    (goal_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["prerequisites"] = self._unjson(data.pop("prerequisites_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in objective list: {e}")
            return []

    def update_mastery(self, objective_id: int, state: str, score: float) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_objectives SET mastery_state = ?, "
                    "mastery_score = ?, updated_at = ? WHERE objective_id = ?",
                    (state, round(score, 1), self._now(), objective_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in objective update_mastery: {e}")
            return False

    def set_status(self, objective_id: int, status: str) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_objectives SET status = ?, updated_at = ? "
                    "WHERE objective_id = ?",
                    (status, self._now(), objective_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in objective set_status: {e}")
            return False

    def set_prerequisites(self, objective_id: int,
                          prerequisites: List[int]) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_objectives SET prerequisites_json = ?, "
                    "updated_at = ? WHERE objective_id = ?",
                    (self._j(prerequisites), self._now(), objective_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in objective set_prerequisites: {e}")
            return False


class CurriculaRepository(_LearningBase):
    def create(self, *, owner_user_id: int, goal_id: int, title: str,
               mode: str = "READ",
               modules: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_curricula "
                    "(owner_user_id, goal_id, title, mode, modules_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner_user_id, goal_id, title, mode,
                     self._j(modules or [])),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in curriculum create: {e}")
            return None

    def get(self, owner_user_id: int, curriculum_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_curricula WHERE curriculum_id = ? "
                    "AND owner_user_id = ?",
                    (curriculum_id, owner_user_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["modules"] = self._unjson(data.pop("modules_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in curriculum get: {e}")
            return None

    def list_for_goal(self, owner_user_id: int,
                      goal_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_curricula WHERE owner_user_id = ? "
                    "AND goal_id = ? ORDER BY curriculum_id",
                    (owner_user_id, goal_id),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["modules"] = self._unjson(data.pop("modules_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in curriculum list: {e}")
            return []

    def update_modules(self, owner_user_id: int, curriculum_id: int,
                       modules: List[Dict[str, Any]], title: Optional[str] = None,
                       mode: Optional[str] = None) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                sets = ["modules_json = ?", "updated_at = ?"]
                params: List[Any] = [self._j(modules), self._now()]
                if title is not None:
                    sets.append("title = ?")
                    params.append(title)
                if mode is not None:
                    sets.append("mode = ?")
                    params.append(mode)
                params += [curriculum_id, owner_user_id]
                cursor.execute(
                    f"UPDATE learning_curricula SET {', '.join(sets)} "
                    "WHERE curriculum_id = ? AND owner_user_id = ?",
                    params,
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in curriculum update: {e}")
            return False


class LessonsRepository(_LearningBase):
    def create(self, *, owner_user_id: int, curriculum_id: int,
               objective_id: int, source_id: Optional[int], grounded: bool,
               content: Dict[str, Any],
               sources: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_lessons "
                    "(owner_user_id, curriculum_id, objective_id, source_id, "
                    "grounded, content_json, sources_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, curriculum_id, objective_id, source_id,
                     1 if grounded else 0, self._j(content), self._j(sources or [])),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in lesson create: {e}")
            return None

    def get_in_curriculum(self, owner_user_id: int, curriculum_id: int,
                          objective_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_lessons WHERE owner_user_id = ? "
                    "AND curriculum_id = ? AND objective_id = ?",
                    (owner_user_id, curriculum_id, objective_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["content"] = self._unjson(data.pop("content_json"), {})
                data["sources"] = self._unjson(data.pop("sources_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in lesson get_in_curriculum: {e}")
            return None

    def get(self, owner_user_id: int, lesson_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_lessons WHERE lesson_id = ? "
                    "AND owner_user_id = ?",
                    (lesson_id, owner_user_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["content"] = self._unjson(data.pop("content_json"), {})
                data["sources"] = self._unjson(data.pop("sources_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in lesson get: {e}")
            return None

    def count_for_owner(self, owner_user_id: int) -> int:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM learning_lessons "
                    "WHERE owner_user_id = ?",
                    (owner_user_id,),
                )
                return int(cursor.fetchone()[0])
        except DB_ERROR as e:
            logger.error(f"Database error in lesson count: {e}")
            return 0


class SessionsRepository(_LearningBase):
    def create(self, *, owner_user_id: int, goal_id: Optional[int],
               objective_id: Optional[int], curriculum_id: Optional[int],
               mode: str = "READ") -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_sessions "
                    "(owner_user_id, goal_id, objective_id, curriculum_id, mode, "
                    "state, step, started_at, last_activity_at) "
                    "VALUES (?, ?, ?, ?, ?, 'STARTED', 'LESSON', ?, ?)",
                    (owner_user_id, goal_id, objective_id, curriculum_id, mode,
                     self._now(), self._now()),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in session create: {e}")
            return None

    def get(self, session_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["result"] = self._unjson(data.pop("result_json"), {})
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in session get: {e}")
            return None

    def resume(self, owner_user_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_sessions WHERE owner_user_id = ? "
                    "AND state != 'COMPLETED' ORDER BY last_activity_at DESC "
                    "LIMIT 1",
                    (owner_user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["result"] = self._unjson(data.pop("result_json"), {})
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in session resume: {e}")
            return None

    def set_step(self, session_id: int, state: str, step: str,
                 lesson_id: Optional[int] = None) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if lesson_id is not None:
                    cursor.execute(
                        "UPDATE learning_sessions SET state = ?, step = ?, "
                        "lesson_id = ?, last_activity_at = ? WHERE session_id = ?",
                        (state, step, lesson_id, self._now(), session_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE learning_sessions SET state = ?, step = ?, "
                        "last_activity_at = ? WHERE session_id = ?",
                        (state, step, self._now(), session_id),
                    )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in session set_step: {e}")
            return False

    def complete(self, session_id: int, result: Dict[str, Any]) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_sessions SET state = 'COMPLETED', "
                    "step = 'COMPLETED', ended_at = ?, result_json = ?, "
                    "last_activity_at = ? WHERE session_id = ?",
                    (self._now(), self._j(result), self._now(), session_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in session complete: {e}")
            return False


class MasteryRepository(_LearningBase):
    def get(self, owner_user_id: int,
            objective_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_mastery WHERE owner_user_id = ? "
                    "AND objective_id = ?",
                    (owner_user_id, objective_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["recent"] = self._unjson(data.pop("recent_json"))
                data["kinds"] = self._unjson(data.pop("kinds_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in mastery get: {e}")
            return None

    def save(self, owner_user_id: int, objective_id: int, state: str,
             score: float, evidence_count: int, recent: List[int],
             kinds: List[str]) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_mastery (owner_user_id, objective_id, "
                    "state, score, evidence_count, recent_json, kinds_json, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(owner_user_id, objective_id) DO UPDATE SET "
                    "state = excluded.state, score = excluded.score, "
                    "evidence_count = excluded.evidence_count, "
                    "recent_json = excluded.recent_json, "
                    "kinds_json = excluded.kinds_json, "
                    "updated_at = excluded.updated_at",
                    (owner_user_id, objective_id, state, round(score, 1),
                     evidence_count, self._j(recent), self._j(kinds), self._now()),
                )
                self._connection().commit()
                return cursor.rowcount >= 0
        except DB_ERROR as e:
            logger.error(f"Database error in mastery save: {e}")
            return False

    def list_for_owner(self, owner_user_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_mastery WHERE owner_user_id = ? "
                    "ORDER BY updated_at DESC",
                    (owner_user_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["recent"] = self._unjson(data.pop("recent_json"))
                    data["kinds"] = self._unjson(data.pop("kinds_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in mastery list: {e}")
            return []

    def delete(self, owner_user_id: int, objective_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "DELETE FROM learning_mastery WHERE owner_user_id = ? "
                    "AND objective_id = ?",
                    (owner_user_id, objective_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in mastery delete: {e}")
            return False


class ReviewsRepository(_LearningBase):
    def create(self, *, owner_user_id: int, objective_id: int, concept: str,
               difficulty: str = "medium", next_review: str) -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO learning_reviews "
                    "(owner_user_id, objective_id, concept, difficulty, next_review) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner_user_id, objective_id, concept, difficulty, next_review),
                )
                self._connection().commit()
                cursor.execute(
                    "SELECT review_id FROM learning_reviews "
                    "WHERE owner_user_id = ? AND objective_id = ? AND concept = ?",
                    (owner_user_id, objective_id, concept),
                )
                row = cursor.fetchone()
                return int(row["review_id"]) if row else None
        except DB_ERROR as e:
            logger.error(f"Database error in review create: {e}")
            return None

    def get(self, owner_user_id: int, objective_id: int,
            concept: str) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_reviews WHERE owner_user_id = ? "
                    "AND objective_id = ? AND concept = ?",
                    (owner_user_id, objective_id, concept),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except DB_ERROR as e:
            logger.error(f"Database error in review get: {e}")
            return None

    def update(self, owner_user_id: int, objective_id: int, concept: str,
               row: Dict[str, Any]) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_reviews SET difficulty = ?, last_reviewed = ?, "
                    "next_review = ?, attempt_count = ?, success_count = ?, "
                    "failure_count = ?, interval_days = ?, ease = ? "
                    "WHERE owner_user_id = ? AND objective_id = ? AND concept = ?",
                    (row.get("difficulty", "medium"), row.get("last_reviewed"),
                     row.get("next_review"), row.get("attempt_count", 0),
                     row.get("success_count", 0), row.get("failure_count", 0),
                     row.get("interval_days", 1), row.get("ease", 2.5),
                     owner_user_id, objective_id, concept),
                )
                self._connection().commit()
                return cursor.rowcount >= 0
        except DB_ERROR as e:
            logger.error(f"Database error in review update: {e}")
            return False

    def due(self, owner_user_id: int, on: str) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT r.*, o.title AS objective_title FROM learning_reviews r "
                    "LEFT JOIN learning_objectives o ON o.objective_id = r.objective_id "
                    "WHERE r.owner_user_id = ? AND r.next_review <= ? "
                    "ORDER BY r.next_review",
                    (owner_user_id, on),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in review due: {e}")
            return []

    def list(self, owner_user_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT r.*, o.title AS objective_title FROM learning_reviews r "
                    "LEFT JOIN learning_objectives o ON o.objective_id = r.objective_id "
                    "WHERE r.owner_user_id = ? ORDER BY r.next_review",
                    (owner_user_id,),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in review list: {e}")
            return []


class AssessmentsRepository(_LearningBase):
    def create(self, *, owner_user_id: int, session_id: Optional[int],
               objective_id: int, kind: str, concept: str, prompt: str,
               options: Optional[List[str]] = None, expected: str = "",
               keywords: Optional[List[str]] = None, user_answer: str = "",
               correct: bool = False, score: float = 0.0,
               feedback: str = "") -> Optional[int]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_assessments "
                    "(owner_user_id, session_id, objective_id, kind, concept, "
                    "prompt, options_json, expected, keywords_json, user_answer, "
                    "correct, score, feedback) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, session_id, objective_id, kind, concept,
                     prompt, self._j(options or []), expected,
                     self._j(keywords or []), user_answer, 1 if correct else 0,
                     round(score, 2), feedback),
                )
                self._connection().commit()
                return cursor.lastrowid
        except DB_ERROR as e:
            logger.error(f"Database error in assessment create: {e}")
            return None

    def list(self, owner_user_id: int, objective_id: Optional[int] = None,
             limit: int = 50) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                if objective_id is not None:
                    cursor.execute(
                        "SELECT * FROM learning_assessments "
                        "WHERE owner_user_id = ? AND objective_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (owner_user_id, objective_id, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM learning_assessments "
                        "WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                        (owner_user_id, limit),
                    )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["options"] = self._unjson(data.pop("options_json"))
                    data["keywords"] = self._unjson(data.pop("keywords_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in assessment list: {e}")
            return []

    def attempts_for_objective(self, owner_user_id: int,
                               objective_id: int) -> List[Dict[str, Any]]:
        return self.list(owner_user_id, objective_id, limit=200)


class MisconceptionsRepository(_LearningBase):
    def record(self, owner_user_id: int, objective_id: int, pattern: str,
               evidence: str, severity: str = "medium") -> bool:
        try:
            item = self.get(owner_user_id, objective_id, pattern)
            with self._manager._lock:
                cursor = self._connection().cursor()
                if item is None:
                    cursor.execute(
                        "INSERT INTO learning_misconceptions "
                        "(owner_user_id, objective_id, pattern, evidence_json, "
                        "count, severity, last_seen) VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (owner_user_id, objective_id, pattern,
                         self._j([evidence]), severity, self._now()),
                    )
                else:
                    evidence_list = self._unjson(item.get("evidence_json"))
                    evidence_list.append(evidence)
                    cursor.execute(
                        "UPDATE learning_misconceptions SET "
                        "evidence_json = ?, count = count + 1, "
                        "severity = ?, last_seen = ? "
                        "WHERE owner_user_id = ? AND objective_id = ? AND pattern = ?",
                        (self._j(evidence_list[-25:]), self._severity(item.get("count", 1) + 1),
                         self._now(), owner_user_id, objective_id, pattern),
                    )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in misconception record: {e}")
            return False

    @staticmethod
    def _severity(count: int) -> str:
        if count >= 4:
            return "high"
        if count >= 2:
            return "medium"
        return "low"

    def get(self, owner_user_id: int, objective_id: int,
            pattern: str) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_misconceptions "
                    "WHERE owner_user_id = ? AND objective_id = ? AND pattern = ?",
                    (owner_user_id, objective_id, pattern),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["evidence"] = self._unjson(data.pop("evidence_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in misconception get: {e}")
            return None

    def list(self, owner_user_id: int) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT m.*, o.title AS objective_title "
                    "FROM learning_misconceptions m "
                    "LEFT JOIN learning_objectives o ON o.objective_id = m.objective_id "
                    "WHERE m.owner_user_id = ? ORDER BY m.count DESC, m.last_seen DESC",
                    (owner_user_id,),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["evidence"] = self._unjson(data.pop("evidence_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in misconception list: {e}")
            return []

    def acknowledge(self, owner_user_id: int, misconception_id: int) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "UPDATE learning_misconceptions SET acknowledged = 1 "
                    "WHERE owner_user_id = ? AND misconception_id = ?",
                    (owner_user_id, misconception_id),
                )
                self._connection().commit()
                return cursor.rowcount > 0
        except DB_ERROR as e:
            logger.error(f"Database error in misconception acknowledge: {e}")
            return False


class PlansRepository(_LearningBase):
    def save(self, owner_user_id: int, plan_date: str,
             items: List[Dict[str, Any]], source: str, total_minutes: int,
             adjusted: bool) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO learning_plans "
                    "(owner_user_id, plan_date, items_json, source, "
                    "total_minutes, adjusted) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner_user_id, plan_date, self._j(items), source,
                     total_minutes, 1 if adjusted else 0),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in plan save: {e}")
            return False

    def get(self, owner_user_id: int, plan_date: str) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_plans WHERE owner_user_id = ? "
                    "AND plan_date = ?",
                    (owner_user_id, plan_date),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data = dict(row)
                data["items"] = self._unjson(data.pop("items_json"))
                return data
        except DB_ERROR as e:
            logger.error(f"Database error in plan get: {e}")
            return None

    def recent(self, owner_user_id: int, days: int = 7) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_plans WHERE owner_user_id = ? "
                    "ORDER BY plan_date DESC LIMIT ?",
                    (owner_user_id, days),
                )
                rows = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data["items"] = self._unjson(data.pop("items_json"))
                    rows.append(data)
                return rows
        except DB_ERROR as e:
            logger.error(f"Database error in plan recent: {e}")
            return []


class LearningProgressRepository(_LearningBase):
    def record(self, owner_user_id: int, record_date: str, **delta: Any) -> bool:
        current = self.get(owner_user_id, record_date)
        base = dict(current) if current else {}
        update = {
            "study_minutes": int(base.get("study_minutes") or 0) + int(delta.get("study_minutes", 0)),
            "practice_count": int(base.get("practice_count") or 0) + int(delta.get("practice_count", 0)),
            "review_count": int(base.get("review_count") or 0) + int(delta.get("review_count", 0)),
            "assessment_count": int(base.get("assessment_count") or 0) + int(delta.get("assessment_count", 0)),
            "assessment_score_avg": delta.get("assessment_score_avg"),
        }
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO learning_progress "
                    "(owner_user_id, record_date, study_minutes, practice_count, "
                    "review_count, assessment_count, assessment_score_avg) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner_user_id, record_date, update["study_minutes"],
                     update["practice_count"], update["review_count"],
                     update["assessment_count"], update["assessment_score_avg"]),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in progress record: {e}")
            return False

    def get(self, owner_user_id: int, record_date: str) -> Optional[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_progress WHERE owner_user_id = ? "
                    "AND record_date = ?",
                    (owner_user_id, record_date),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except DB_ERROR as e:
            logger.error(f"Database error in progress get: {e}")
            return None

    def range(self, owner_user_id: int, start: str, end: str) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_progress WHERE owner_user_id = ? "
                    "AND record_date BETWEEN ? AND ? ORDER BY record_date",
                    (owner_user_id, start, end),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in progress range: {e}")
            return []


class LearningEventsRepository(_LearningBase):
    def log(self, owner_user_id: int, action: str,
            objective_id: Optional[int] = None, session_id: Optional[int] = None,
            detail: Optional[Dict[str, Any]] = None) -> bool:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "INSERT INTO learning_events (owner_user_id, action, "
                    "objective_id, session_id, detail_json) VALUES (?, ?, ?, ?, ?)",
                    (owner_user_id, action, objective_id, session_id,
                     self._j(detail) if detail else None),
                )
                self._connection().commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in learning event log: {e}")
            return False

    def list(self, owner_user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            with self._manager._lock:
                cursor = self._connection().cursor()
                cursor.execute(
                    "SELECT * FROM learning_events WHERE owner_user_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (owner_user_id, limit),
                )
                return [dict(r) for r in cursor.fetchall()]
        except DB_ERROR as e:
            logger.error(f"Database error in learning event list: {e}")
            return []


class LearningStore:
    """Aggregate facade over every learning repository."""

    def __init__(self, manager: DatabaseManager):
        self.goals = EducationalGoalsRepository(manager)
        self.objectives = LearningObjectivesRepository(manager)
        self.curricula = CurriculaRepository(manager)
        self.lessons = LessonsRepository(manager)
        self.sessions = SessionsRepository(manager)
        self.mastery = MasteryRepository(manager)
        self.reviews = ReviewsRepository(manager)
        self.assessments = AssessmentsRepository(manager)
        self.misconceptions = MisconceptionsRepository(manager)
        self.plans = PlansRepository(manager)
        self.progress = LearningProgressRepository(manager)
        self.events = LearningEventsRepository(manager)
        self._manager = manager

    _OWNER_TABLES = (
        "learning_events",
        "learning_progress",
        "learning_plans",
        "learning_misconceptions",
        "learning_assessments",
        "learning_reviews",
        "learning_mastery",
        "learning_sessions",
        "learning_lessons",
        "learning_curricula",
        "learning_objectives",
        "learning_goals",
    )

    def clear_owner(self, owner_user_id: int) -> bool:
        """Privacy: erase every learning record for the owner."""
        try:
            with self._manager._lock:
                connection = self._manager._get_connection()
                cursor = connection.cursor()
                for table in self._OWNER_TABLES:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE owner_user_id = ?",
                        (owner_user_id,),
                    )
                connection.commit()
                return True
        except DB_ERROR as e:
            logger.error(f"Database error in learning clear_owner: {e}")
            return False


__all__ = ["LearningStore"]