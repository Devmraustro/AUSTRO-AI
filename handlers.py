"""
AUSTRO AI - Handlers shim.

Phase B: all Telegram handlers live in `app.telegram.handlers`. This module
re-exports the public surface so `from handlers import get_*_handlers` and
constant imports keep working unchanged.
"""

from app.telegram.handlers import (
    REVIEW_ACCOMPLISHED,
    get_accountability_handlers,
    get_english_handlers,
    get_goal_handlers,
    get_habit_handlers,
    get_learning_handlers,
    get_menu_handlers,
    get_plan_handlers,
    get_programming_handlers,
    get_registration_handlers,
    get_reminder_handlers,
    get_study_handlers,
)

__all__ = [
    "REVIEW_ACCOMPLISHED",
    "get_registration_handlers",
    "get_goal_handlers",
    "get_plan_handlers",
    "get_study_handlers",
    "get_english_handlers",
    "get_programming_handlers",
    "get_accountability_handlers",
    "get_habit_handlers",
    "get_reminder_handlers",
    "get_learning_handlers",
    "get_menu_handlers",
]