"""AUSTRO AI - reminder lifecycle regressions.

REMINDER-1 (functional, user-visible)
    `reminder_confirm` told the user "your reminder is saved and will be sent at
    the chosen time", but nothing scheduled it: the scheduler only reconciles on
    startup and once per hour, so a reminder created for the next few minutes
    was silently skipped. It is now activated immediately after the row is
    written.

REMINDER-2 (functional, user-visible, repeat-delivery bug)
    Nothing in the application ever wrote `reminders.is_active`. A one-off
    reminder therefore stayed active forever, and every hourly resync created a
    fresh `run_once` job for a timestamp that had already passed - so one
    "remind me at 20:00" was re-delivered every hour for as long as the bot ran.
    Delivery now retires the one-off reminder.

REMINDER-3 (data correctness)
    An unparseable time fell back to "now" and still reported success, storing a
    reminder at an arbitrary time. It is now rejected so the user can correct
    the typo, and the seconds/microseconds of "now" are no longer copied into
    the daily schedule.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["GEMINI_API_KEY"] = ""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.reminders import ReminderService, parse_time_string
from app.core.errors import ValidationError
from app.infrastructure.scheduler import MAX_ONE_OFF_DELIVERY_ATTEMPTS, ReminderScheduler
from app.telegram import handlers as H
from database import db


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeJob:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs_list = []

    def jobs(self):
        return list(self.jobs_list)

    def _mk(self, name, **kwargs):
        job = FakeJob(name, **kwargs)
        self.jobs_list.append(job)
        return job

    def run_daily(self, callback, time=None, name=None, data=None):
        return self._mk(name, time=time, data=data, callback=callback)

    def run_once(self, callback, when=None, name=None, data=None):
        return self._mk(name, when=when, data=data, callback=callback)

    def run_repeating(self, callback, interval=None, first=None, name=None):
        return self._mk(name, interval=interval, first=first, callback=callback)


class FakeApp:
    def __init__(self):
        self.job_queue = FakeJobQueue()
        self.bot_data = {}


class _FakeJob:
    def __init__(self, data):
        self.data = data


class _FakeContext:
    def __init__(self, data=None, container=None, user_data=None, bot_data=None):
        self.job = _FakeJob(data or {})
        self.bot = Mock(send_message=AsyncMock(return_value=None))
        self.bot_data = bot_data if bot_data is not None else {"container": container}
        self.user_data = dict(user_data or {})


class _Query:
    def __init__(self):
        self.data = "reminder_confirm"
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _User:
    id = 77


class _Update:
    def __init__(self):
        self.callback_query = _Query()
        self.effective_user = _User()
        self.message = None


def _reminder_row(reminder_id):
    conn = db._get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,))
    return cursor.fetchone()


def _services(**reminder_methods):
    reminders = Mock()
    reminders.create_daily_reminder = Mock(return_value=True)
    for name, value in reminder_methods.items():
        setattr(reminders, name, value)
    return Mock(reminders=reminders)


# ---------------------------------------------------------------------------
# REMINDER-1: creation takes effect immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_confirm_activates_the_scheduler():
    scheduler = Mock(resync=AsyncMock(return_value=None))
    services = _services()
    update = _Update()
    context = _FakeContext(
        container=services,
        user_data={"reminder_title": "t", "reminder_message": "m", "reminder_time": "08:30"},
        bot_data={"container": services, "reminder_scheduler": scheduler},
    )

    result = await H.reminder_confirm(update, context)

    services.reminders.create_daily_reminder.assert_called_once()
    scheduler.resync.assert_awaited_once()
    assert result == H.ConversationHandler.END
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "تم إنشاء التذكير" in text
    assert "سيُجدول" not in text  # no "pending" hedge when activation worked


@pytest.mark.asyncio
async def test_reminder_confirm_reports_when_no_scheduler_is_attached():
    """Without a scheduler the row still exists; do not promise instant delivery."""
    services = _services()
    update = _Update()
    context = _FakeContext(
        container=services,
        user_data={"reminder_time": "08:30"},
    )

    await H.reminder_confirm(update, context)

    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "سيُجدول عند أول تحديث تلقائي" in text


@pytest.mark.asyncio
async def test_a_resync_failure_does_not_break_the_confirmation():
    scheduler = Mock(resync=AsyncMock(side_effect=RuntimeError("boom")))
    services = _services()
    update = _Update()
    context = _FakeContext(
        container=services,
        user_data={"reminder_time": "08:30"},
        bot_data={"container": services, "reminder_scheduler": scheduler},
    )

    await H.reminder_confirm(update, context)

    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "تم إنشاء التذكير" in text


# ---------------------------------------------------------------------------
# REMINDER-2: delivery retires a one-off reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_off_reminder_is_delivered_once_and_retired():
    db.create_user(20, "u", "U")
    reminder_id = db.create_reminder(
        20, "custom", "t", "اشرب الماء", "2026-10-01T08:00:00", False
    )
    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()

    job = next(
        j for j in scheduler.application.job_queue.jobs_list
        if j.name == f"custom_reminder_{reminder_id}"
    )
    assert job.kwargs["data"]["reminder_id"] == reminder_id
    assert job.kwargs["data"]["is_recurring"] is False

    context = _FakeContext(data=job.kwargs["data"])
    await job.kwargs["callback"](context)

    context.bot.send_message.assert_awaited_once()
    assert int(_reminder_row(reminder_id)["is_active"]) == 0

    # The next hourly resync must NOT re-create the job.
    scheduler.application.job_queue.jobs_list.clear()
    await scheduler.resync()
    names = [j.name for j in scheduler.application.job_queue.jobs_list]
    assert f"custom_reminder_{reminder_id}" not in names


@pytest.mark.asyncio
async def test_recurring_reminder_stays_active_after_delivery():
    db.create_user(21, "u", "U")
    reminder_id = db.create_reminder(
        21, "custom", "t", "تمرين", "2026-10-02T09:00:00", True
    )
    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()

    job = next(
        j for j in scheduler.application.job_queue.jobs_list
        if j.name == f"custom_reminder_{reminder_id}"
    )
    assert job.kwargs["data"]["is_recurring"] is True
    await job.kwargs["callback"](_FakeContext(data=job.kwargs["data"]))

    assert int(_reminder_row(reminder_id)["is_active"]) == 1


@pytest.mark.asyncio
async def test_failed_delivery_keeps_the_one_off_reminder_pending():
    """A delivery failure must not silently drop the reminder."""
    db.create_user(22, "u", "U")
    reminder_id = db.create_reminder(
        22, "custom", "t", "مهم", "2026-10-01T08:00:00", False
    )
    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()
    job = next(
        j for j in scheduler.application.job_queue.jobs_list
        if j.name == f"custom_reminder_{reminder_id}"
    )

    failing = Mock()
    failing.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    context = _FakeContext(data=job.kwargs["data"])
    context.bot = failing

    await job.kwargs["callback"](context)

    row = _reminder_row(reminder_id)
    assert int(row["is_active"]) == 1
    assert int(row["delivery_attempts"]) == 1


@pytest.mark.asyncio
async def test_one_off_reminder_is_retired_after_max_failed_attempts():
    db.create_user(24, "u", "U")
    reminder_id = db.create_reminder(
        24, "custom", "t", "مهم", "2026-10-01T08:00:00", False
    )
    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True

    for attempt in range(1, MAX_ONE_OFF_DELIVERY_ATTEMPTS + 1):
        scheduler.application.job_queue.jobs_list.clear()
        await scheduler.resync()
        job = next(
            j for j in scheduler.application.job_queue.jobs_list
            if j.name == f"custom_reminder_{reminder_id}"
        )
        failing = Mock()
        failing.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
        context = _FakeContext(data=job.kwargs["data"])
        context.bot = failing
        await job.kwargs["callback"](context)

        row = _reminder_row(reminder_id)
        assert int(row["delivery_attempts"]) == attempt
        assert int(row["is_active"]) == int(attempt < MAX_ONE_OFF_DELIVERY_ATTEMPTS)

    scheduler.application.job_queue.jobs_list.clear()
    await scheduler.resync()
    assert not any(
        job.name == f"custom_reminder_{reminder_id}"
        for job in scheduler.application.job_queue.jobs_list
    )


# ---------------------------------------------------------------------------
# REMINDER-3: honest time handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "25:00", "08:60", "abc", "8", "08:00:00"])
def test_invalid_times_are_rejected(bad):
    service = ReminderService(db)
    with pytest.raises(ValidationError):
        service.create_daily_reminder(20, "t", "m", bad)


def test_created_reminder_drops_seconds_and_microseconds():
    db.create_user(23, "u", "U")
    service = ReminderService(db)
    before = datetime.now()
    assert service.create_daily_reminder(23, "t", "m", "07:45") is True

    rows = db.reminders.list_active(23)
    assert rows, "the reminder must be stored"
    stored = datetime.fromisoformat(rows[-1]["scheduled_time"])
    assert (stored.hour, stored.minute) == (7, 45)
    assert stored.second == 0 and stored.microsecond == 0
    assert stored.date() in (before.date(), before.date())


@pytest.mark.asyncio
async def test_reminder_confirm_reasks_on_bad_time_instead_of_claiming_success():
    services = _services()
    services.reminders.create_daily_reminder = Mock(
        side_effect=ValidationError("bad time")
    )
    update = _Update()
    context = _FakeContext(container=services, user_data={"reminder_time": "25:00"})

    result = await H.reminder_confirm(update, context)

    assert result == H.REMINDER_TIME  # stays in the flow to retype
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "HH:MM" in text


def test_parse_time_string_accepts_only_hh_mm():
    assert parse_time_string("08:30") == (8, 30)
    assert parse_time_string("0:05") == (0, 5)
    for bad in ("", "24:00", "12:60", "-1:00", "aa:bb", "12"):
        assert parse_time_string(bad) is None
