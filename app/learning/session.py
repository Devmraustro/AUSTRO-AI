"""AUSTRO AI - Learning session manager (Phase E).

Sessions are state machines: STARTED -> LESSON -> PRACTICE -> ASSESS ->
REVIEW -> COMPLETED. A session always belongs to one objective + curriculum
and can be resumed (the linter-inactive step is preserved). Resume surfaces
the exact stored step, never a fresh one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.learning.models import LearningSession
from app.learning.repositories import LearningStore

logger = logging.getLogger(__name__)

_SESSION_FLOW = ("LESSON", "PRACTICE", "ASSESS", "REVIEW", "COMPLETED")


def _as_session(row: Dict[str, Any]) -> LearningSession:
    known = {f for f in LearningSession.__dataclass_fields__}
    return LearningSession(**{k: v for k, v in row.items() if k in known})


class SessionManager:
    def __init__(self, store: LearningStore):
        self._store = store

    def start(self, owner_user_id: int, objective_id: int,
              goal_id: Optional[int] = None,
              curriculum_id: Optional[int] = None,
              mode: str = "READ") -> Optional[LearningSession]:
        session_id = self._store.sessions.create(
            owner_user_id=owner_user_id, goal_id=goal_id,
            objective_id=objective_id, curriculum_id=curriculum_id, mode=mode,
        )
        if session_id is None:
            return None
        row = self._store.sessions.get(session_id)
        session = _adorn(row)
        self._store.events.log(
            owner_user_id, "session_started", objective_id, session_id,
            {"mode": mode},
        )
        return session

    def attach_lesson(self, session: LearningSession, lesson_id: int) -> bool:
        return self._store.sessions.set_step(
            session.session_id, "LESSON", "LESSON", lesson_id=lesson_id,
        )

    def advance(self, session: LearningSession, to_step: str,
                state: Optional[str] = None) -> LearningSession:
        if to_step not in _SESSION_FLOW:
            to_step = _SESSION_FLOW[0]
        self._store.sessions.set_step(
            session.session_id, state or to_step, to_step,
        )
        return self.resume(session.owner_user_id) or session

    def resume(self, owner_user_id: int) -> Optional[LearningSession]:
        row = self._store.sessions.resume(owner_user_id)
        if row is None:
            return None
        return _adorn(row)

    def get(self, session_id: int) -> Optional[LearningSession]:
        row = self._store.sessions.get(session_id)
        return _adorn(row) if row else None

    def complete(self, session: LearningSession, *,
                 minutes: int = 0) -> LearningSession:
        result = {"study_minutes": minutes}
        self._store.sessions.complete(session.session_id, result)
        if session.owner_user_id and minutes:
            from datetime import datetime
            day = datetime.utcnow().strftime("%Y-%m-%d")
            self._store.progress.record(session.owner_user_id, day,
                                        study_minutes=minutes)
        self._store.events.log(
            session.owner_user_id, "session_completed",
            session.objective_id, session.session_id,
            {"minutes": minutes},
        )
        return session


def _adorn(row: Dict[str, Any]) -> LearningSession:
    return _as_session(row)


__all__ = ["SessionManager", "_as_session"]