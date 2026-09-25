"""AUSTRO AI - Domain entities for write operations.

Read paths still return plain dicts (faithful to the SQLite schema) so the
presentation layer can render them exactly as before; these DTOs are used for
the create flows so services expose typed signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NewGoal:
    user_id: int
    title: str
    description: str
    category: str
    stages: List[str] = field(default_factory=list)
    deadline: Optional[str] = None


@dataclass
class NewPlan:
    user_id: int
    date: str
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    priorities: List[str] = field(default_factory=list)
    review_time: str = ""
    break_time: str = ""


@dataclass
class NewHabit:
    user_id: int
    name: str
    description: str
    frequency: str = "daily"
    reminder_time: str = "08:00"


@dataclass
class DailyReviewData:
    user_id: int
    date: str
    accomplished: str = ""
    learned: str = ""
    obstacles: str = ""
    tomorrow_plan: str = ""
    mood: int = 3


@dataclass
class CustomReminder:
    user_id: int
    title: str
    message: str
    scheduled_time: str
    is_recurring: bool = True
    frequency: str = "daily"