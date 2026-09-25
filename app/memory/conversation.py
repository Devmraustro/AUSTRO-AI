"""AUSTRO AI - In-memory conversation store.

Keeps a bounded per-user exchange window so the AI can answer with context.
Not persisted; lives for the lifetime of the process (same semantics as the
original AIEngine history).
"""

from __future__ import annotations

import threading
from typing import Dict, List

_EXCHANGE_LIMIT = 10
_CONTEXT_WINDOW = 3


class ConversationMemory:
    """Thread-safe bounded per-user conversation history."""

    def __init__(self, exchange_limit: int = _EXCHANGE_LIMIT):
        self._history: Dict[int, List[Dict[str, str]]] = {}
        self._limit = exchange_limit
        self._lock = threading.Lock()

    def add(self, user_id: int, user_text: str, ai_text: str) -> None:
        with self._lock:
            self._history.setdefault(user_id, []).append(
                {"user": user_text, "ai": ai_text}
            )
            if len(self._history[user_id]) > self._limit:
                self._history[user_id] = self._history[user_id][-self._limit:]

    def recent(self, user_id: int, window: int = _CONTEXT_WINDOW) -> List[Dict[str, str]]:
        with self._lock:
            history = list(self._history.get(user_id, []))
        return history[-window:]

    def reset(self, user_id: int) -> None:
        with self._lock:
            self._history.pop(user_id, None)

    def clear(self) -> None:
        with self._lock:
            self._history.clear()