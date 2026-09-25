"""AUSTRO AI - Memory retrieval and ranking.

Bounded, deterministic retrieval builds a short context pack for the AI:
- Loads only a bounded window of the user's active memories (never all).
- Scores each with importance, confidence, recency, lexical relevance to the
  current task and a type boost for standing instructions.
- Low-confidence memories are NEVER treated as facts: they only surface when
  the current request textually overlaps them.
- The final pack caps at ``budget_chars`` and is rendered as labelled DATA,
  so memory can inform but never override the current user request.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from app.config.settings import Settings
from app.memory.models import (
    MemoryContextPack,
    MemoryItem,
    TaskContext,
    split_words,
)
from app.memory.repositories import MemoryStore

logger = logging.getLogger(__name__)

_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.55, "low": 0.15}
_TYPE_BOOST = {
    "user_instruction": 0.35,
    "important_context": 0.30,
    "communication_preference": 0.25,
    "coaching_preference": 0.25,
    "goal": 0.20,
    "achievement": 0.10,
}
_HOLD_TYPES = ("user_instruction", "goal", "achievement",
               "communication_preference", "coaching_preference")


def _date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


class MemoryRetrieval:
    """Scores and bounds memory for a given task context."""

    def __init__(self, store: MemoryStore, settings_: Settings):
        self.store = store
        self.settings = settings_

    def relevant(self, owner_user_id: int, task: Optional[TaskContext] = None,
                 *, top_k: Optional[int] = None,
                 budget_chars: Optional[int] = None,
                 purpose: str = "context_pack") -> MemoryContextPack:
        task = task or TaskContext()
        top_k = top_k or self.settings.memory_max_retrieved
        budget = budget_chars or self.settings.memory_context_budget_chars

        window = self.store.memories.list(
            owner_user_id, status="active", limit=self.settings.memory_max_candidates,
        )
        keywords = task.keywords()
        scored = []
        for memory in window:
            score, eligible = self._score(memory, keywords)
            if not eligible:
                continue
            scored.append((score, memory))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        selected = [memory for _, memory in scored[:top_k]]
        used_ids = [int(m.memory_id) for m in selected if m.memory_id]
        for memory_id in used_ids:
            self.store.memories.touch(owner_user_id, memory_id)
        for memory_id in used_ids:
            self.store.access.log(owner_user_id=owner_user_id,
                                  memory_id=memory_id, purpose=purpose)

        pack = MemoryContextPack(
            memories=selected,
            used_memory_ids=used_ids,
            count_before_rank=len(window),
            budget_chars=budget,
        )
        pack.render(budget)
        return pack

    def _score(self, memory: MemoryItem, keywords: List[str]) -> tuple:
        """Return (score, eligible). Ineligible memories are skipped."""
        importance_w = min(memory.importance, 5) / 5.0
        confidence_w = _CONFIDENCE_WEIGHT.get(memory.confidence, 0.3)

        # Low confidence is never a fact: only shows when the current request
        # explicitly mentions the same words.
        relevance = self._relevance(memory, keywords)
        if memory.confidence == "low" and relevance < 0.5:
            return 0.0, False

        recency_w = self._recency_weight(memory)
        type_boost = _TYPE_BOOST.get(memory.memory_type, 0.0)

        score = (
            0.30 * importance_w
            + 0.25 * confidence_w
            + 0.15 * recency_w
            + 0.20 * relevance
            + 0.10 * type_boost
            - self._event_penalty(memory, relevance)
        )
        return round(max(0.0, score), 4), True

    @staticmethod
    def _relevance(memory: MemoryItem, keywords: List[str]) -> float:
        if not keywords:
            return 0.0
        words = split_words(f"{memory.subject} {memory.claim}")
        if not words:
            return 0.0
        overlap = sum(1 for w in set(words) if w in set(keywords))
        return min(1.0, overlap / max(1, len(set(keywords)) / 2.0))

    @staticmethod
    def _recency_weight(memory: MemoryItem) -> float:
        if memory.decay_policy in ("hold", "stable"):
            return 1.0
        updated = _date(memory.updated_at) or _date(memory.created_at)
        if updated is None:
            return 0.5
        days = max(0.0, (datetime.utcnow() - updated).total_seconds() / 86400.0)
        if memory.decay_policy == "event":
            return max(0.0, 1.0 - days / 14.0)
        return max(0.2, 1.0 - days / 180.0)

    @staticmethod
    def _event_penalty(memory: MemoryItem, relevance: float) -> float:
        """Report-style event memories fade unless the topic is in scope."""
        if memory.memory_type != "episodic_event":
            return 0.0
        return 0.0 if relevance >= 0.5 else 0.15