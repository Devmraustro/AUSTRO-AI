"""AUSTRO AI - Identity / context DTOs used by services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserContext:
    """Minimal identity of an interacting Telegram user."""

    user_id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""