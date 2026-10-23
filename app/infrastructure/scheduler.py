"""
AUSTRO AI - Smart reminder scheduler.

Async scheduler integrated with the python-telegram-bot job queue. Uses a
constructor-injected database (defaults to the shared instance) so it can be
unit-tested with fakes and does not import a global.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, time, timedelta
from typing import Optional

from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from app.config.prompts import REMINDER_TIMES
from app.database import db as default_db
from app.database.repositories import Database

REMINDER_TIMES_FORMAT = {key: time.fromisoformat(value) for key, value in REMINDER_TIMES.items()}

logger = logging.getLogger(__name__)

# One-off reminders are retried during hourly reconciliation, but never forever.
MAX_ONE_OFF_DELIVERY_ATTEMPTS = 5


class ReminderScheduler:
    """Async reminder scheduler integrated with PTB job queue."""

    def __init__(self, application: Application, database: Optional[Database] = None):
        self.application = application
        self._db = database or default_db
        self.running = False

    async def start(self) -> None:
        """Start the scheduler - sets up periodic jobs."""
        self.running = True
        logger.info("Reminder scheduler started")

        self.application.job_queue.run_repeating(
            self._check_reminders,
            interval=timedelta(hours=1),
            first=10,
            name="reminder_checker",
        )

        self._schedule_fixed_reminders()

        await self.resync()

    def _schedule_fixed_reminders(self) -> None:
        """Schedule reminders at fixed times."""
        self.application.job_queue.run_daily(
            self._send_morning_reminders,
            time=REMINDER_TIMES_FORMAT["morning"],
            name="morning_reminder",
        )
        self.application.job_queue.run_daily(
            self._send_noon_reminders,
            time=REMINDER_TIMES_FORMAT["noon"],
            name="noon_reminder",
        )
        self.application.job_queue.run_daily(
            self._send_evening_reminders,
            time=REMINDER_TIMES_FORMAT["evening"],
            name="evening_reminder",
        )
        self.application.job_queue.run_daily(
            self._send_night_reminders,
            time=REMINDER_TIMES_FORMAT["night_review"],
            name="night_reminder",
        )
        self.application.job_queue.run_daily(
            self._send_random_motivation,
            time=time(hour=15, minute=0),
            name="motivation_reminder",
        )

    async def _check_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reconcile database reminders with the job queue and send due ones."""
        try:
            await self.resync()
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            logger.error(f"Error checking reminders: {e}")

    async def resync(self) -> None:
        """Load active reminders from the DB and schedule jobs for them."""
        if not self.running or not self.application.job_queue:
            return

        reminders = self._db.get_all_active_reminders()
        active_ids = {rem["reminder_id"] for rem in reminders}

        existing = [
            job for job in self.application.job_queue.jobs()
            if job.name and job.name.startswith("custom_reminder_")
        ]
        for job in existing:
            rid = int(job.name.rsplit("_", 1)[1])
            if rid not in active_ids:
                job.schedule_removal()

        for rem in reminders:
            reminder_id = rem["reminder_id"]
            job_name = f"custom_reminder_{reminder_id}"
            if any(job.name == job_name for job in self.application.job_queue.jobs()):
                continue

            try:
                when = datetime.fromisoformat(rem["scheduled_time"])
                title = rem.get("title") or ""
                reminder_text = rem.get("message") or title
                data = {
                    "user_id": rem["user_id"],
                    "message": reminder_text,
                    "reminder_id": reminder_id,
                    "is_recurring": bool(rem.get("is_recurring")),
                }

                if rem.get("is_recurring"):
                    self.application.job_queue.run_daily(
                        self._send_custom_reminder,
                        time=when.time(),
                        name=job_name,
                        data=data,
                    )
                else:
                    self.application.job_queue.run_once(
                        self._send_custom_reminder,
                        when=when,
                        name=job_name,
                        data=data,
                    )
            except (ValueError, TypeError) as e:
                logger.error(f"Could not schedule reminder {reminder_id}: {e}")

    async def _send_morning_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send morning reminders to all active users."""
        try:
            for user_id in self._db.get_all_user_ids():
                await self.send_morning_reminder(user_id, context)
            logger.info("Sent morning reminders")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error sending morning reminders: {e}")

    async def _send_noon_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send noon reminders."""
        try:
            for user_id in self._db.get_all_user_ids():
                await self.send_motivation(user_id, context)
            logger.info("Sent noon reminders")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error sending noon reminders: {e}")

    async def _send_evening_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send evening reminders."""
        try:
            for user_id in self._db.get_all_user_ids():
                await self.send_habit_reminder(user_id, "مراجعة إنجازات اليوم", context)
            logger.info("Sent evening reminders")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error sending evening reminders: {e}")

    async def _send_night_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send night review reminders."""
        try:
            for user_id in self._db.get_all_user_ids():
                await self.send_night_reminder(user_id, context)
            logger.info("Sent night reminders")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error sending night reminders: {e}")

    async def _send_random_motivation(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send random motivation to users."""
        try:
            for user_id in self._db.get_all_user_ids():
                await self.send_motivation(user_id, context)
            logger.info("Sent motivation reminders")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error sending motivation: {e}")

    async def send_morning_reminder(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send morning reminder to a specific user."""
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "☀️ **صباح الإنجاز!**\n\n"
                    "🎯 راجع أهدافك لليوم\n"
                    "📅 تحقق من خطتك\n"
                    "💪 ابدأ بأصعب مهمة\n\n"
                    "لنبدأ يوماً منتجاً! 🔥"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to send morning reminder to {user_id}: {e}")

    async def send_night_reminder(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send night review reminder to a specific user."""
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🌙 **وقت المحاسبة!**\n\n"
                    "قبل النوم، أجب على:\n"
                    "1️⃣ ماذا أنجزت؟\n"
                    "2️⃣ ماذا تعلمت؟\n"
                    "3️⃣ ما العقبات؟\n"
                    "4️⃣ ما خطة الغد؟\n\n"
                    "استخدم /review للبدء! 📝"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to send night reminder to {user_id}: {e}")

    async def send_habit_reminder(self, user_id: int, habit_name: str,
                                  context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send habit reminder to a specific user."""
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🔄 **تذكير بالعادة!**\n\n"
                    f"لا تنسَ: {habit_name}\n\n"
                    "هل أكملتها اليوم؟ ✅\n\n"
                    "استمر! الزخم يبني النجاح! 💪"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to send habit reminder to {user_id}: {e}")

    async def send_motivation(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random motivation to a specific user."""
        motivations = [
            "💪 **نصيحة اليوم:**\n\nابدأ بأصغر مهمة. الزخم سيأتي!",
            "🎯 **تذكير:**\n\nكل يوم تتعلم فيه شيئاً جديداً هو يوم ناجح!",
            "⚡ **تحفيز:**\n\nلا تستسلم! أنت أقرب مما تظن!",
            '🔥 **اقتباس:**\n\n"النجاح ليس نهائياً والفشل ليس قاتلاً، ما يهم هو الشجاعة للاستمرار."',
            "🌟 **تذكير:**\n\nاحتفل بإنجازاتك الصغيرة. كل خطوة تُحسب!",
        ]
        message = random.choice(motivations)
        try:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to send motivation to {user_id}: {e}")

    def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        logger.info("Reminder scheduler stopped")

    async def schedule_user_reminder(self, user_id: int, reminder_type: str,
                                     title: str, message: str,
                                     when: datetime, context: ContextTypes.DEFAULT_TYPE,
                                     is_recurring: bool = False) -> bool:
        """Schedule a custom reminder for a user."""
        try:
            self._db.create_reminder(
                user_id=user_id,
                reminder_type=reminder_type,
                title=title,
                message=message,
                scheduled_time=when.isoformat(),
                is_recurring=is_recurring,
            )
            await self.resync()
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error scheduling reminder: {e}")
            return False

    async def _send_custom_reminder(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a custom reminder, then retire it if it was a one-off."""
        job_data = context.job.data or {}
        user_id = job_data.get("user_id")
        message = job_data.get("message") or "تذكير من AUSTRO AI"

        delivered = False
        try:
            await context.bot.send_message(
                chat_id=user_id, text=message, parse_mode="Markdown"
            )
            delivered = True
        except TelegramError:
            # Markdown in user-supplied text can be invalid; retry as plain text.
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
                delivered = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to send custom reminder to {user_id}: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to send custom reminder to {user_id}: {e}")

        reminder_id = job_data.get("reminder_id")
        is_recurring = bool(job_data.get("is_recurring"))
        if reminder_id is not None and not is_recurring:
            if delivered:
                # A one-off reminder must leave the active set after success.
                self._db.deactivate_reminder(int(reminder_id))
            else:
                # Persist the count so restarts cannot reset the retry budget.
                attempts = self._db.record_reminder_delivery_failure(
                    int(reminder_id), MAX_ONE_OFF_DELIVERY_ATTEMPTS
                )
                if attempts is None:
                    logger.error(
                        "Could not persist failed delivery attempt for reminder %s",
                        reminder_id,
                    )
                elif attempts >= MAX_ONE_OFF_DELIVERY_ATTEMPTS:
                    logger.error(
                        "Deactivated one-off reminder %s after %s failed delivery attempts",
                        reminder_id, attempts,
                    )
                else:
                    logger.warning(
                        "One-off reminder %s delivery failed (%s/%s); "
                        "will be retried by scheduler reconciliation",
                        reminder_id, attempts, MAX_ONE_OFF_DELIVERY_ATTEMPTS,
                    )