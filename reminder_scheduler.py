"""
AUSTRO AI - Reminder scheduler shim.

Phase B: the scheduler lives in `app.infrastructure.scheduler`. This module
re-exports `ReminderScheduler` so old imports keep working unchanged.
"""

from app.infrastructure.scheduler import ReminderScheduler  # noqa: F401

__all__ = ["ReminderScheduler"]