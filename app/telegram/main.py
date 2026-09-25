"""
AUSTRO AI - Telegram Bot
Main Application (PRESENTATION WIRING / COMPOSITION ROOT).

Builds the fully-wired python-telegram-bot application: command handlers,
conversation handlers, menu handlers, the reminder scheduler hooks and the
service container (stored in `context.bot_data["container"]`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    filters,
)

from app.config.prompts import ARABIC_RESPONSES
from app.config.settings import settings
from app.core.container import build_container, get_services
from app.core.errors import public_message
from app.domain.context import UserContext
from app.infrastructure.scheduler import ReminderScheduler
from app.observability.logging_config import setup_logging
from app.telegram.handlers import (
    _knowledge_menu_content_for,
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
    knowledge_document,
    knowledge_question,
    memory_text,
)

# Pin console + file logging once, with redaction.
setup_logging(settings)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user = update.effective_user
    user_id = user.id
    services = get_services(context)

    try:
        existing_user = services.users.profile(user_id)

        if existing_user and existing_user.get("age"):
            # Returning user - show main menu
            keyboard = [
                [InlineKeyboardButton("🎯 أهدافي", callback_data="menu_goals")],
                [InlineKeyboardButton("📅 خطتي اليومية", callback_data="menu_plan")],
                [InlineKeyboardButton("⚡ وضع الانضباط", callback_data="menu_discipline")],
                [InlineKeyboardButton("🧠 رحلة التعلم", callback_data="menu_learn")],
                [InlineKeyboardButton("📚 وضع الدراسة", callback_data="menu_study")],
                [InlineKeyboardButton("🇬🇧 وضع الإنجليزية", callback_data="menu_english")],
                [InlineKeyboardButton("💻 وضع البرمجة", callback_data="menu_programming")],
                [InlineKeyboardButton("🔄 بناء العادات", callback_data="menu_habits")],
                [InlineKeyboardButton("📊 لوحة التحكم", callback_data="menu_dashboard")],
                [InlineKeyboardButton("🧠 المدرب الذكي", callback_data="menu_coach")],
                [InlineKeyboardButton("🌙 المحاسبة اليومية", callback_data="menu_review")],
                [InlineKeyboardButton("📚 المعرفة", callback_data="menu_knowledge")],
                [InlineKeyboardButton("🧠 ذاكرتي", callback_data="menu_memory")],
                [InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"مرحباً بعودتك {user.first_name}! 👋\n\n"
                f"أنا AUSTRO AI، جاهز لمساعدتك اليوم! 💪\n\n"
                f"اختر ما تريد:",
                reply_markup=reply_markup
            )
        else:
            # New user - start registration
            services.users.ensure_user(
                UserContext(user_id, user.username or "", user.first_name, user.last_name or "")
            )

            keyboard = [[InlineKeyboardButton("🚀 ابدأ التسجيل", callback_data="start_registration")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                ARABIC_RESPONSES["welcome"],
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
    except Exception as e:  # noqa: BLE001 - keep /start resilient
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text(
            "❌ عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى أو التواصل مع الدعم."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command"""
    help_text = (
        "🆘 **المساعدة**\n\n"
        "الأوامر المتاحة:\n"
        "/start - بدء البوت\n"
        "/help - المساعدة\n"
        "/plan - خطة اليوم\n"
        "/goals - أهدافي\n"
        "/habits - عاداتي\n"
        "/progress - تقدمي\n"
        "/review - المحاسبة اليومية\n"
        "/coach - المدرب الذكي\n"
        "/dashboard - لوحة التحكم\n"
        "/settings - الإعدادات\n\n"
        "للمساعدة: تواصل مع @your_support"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to today's plan"""
    user_id = update.effective_user.id
    services = get_services(context)
    plan = services.plans.for_date(user_id, datetime.now().strftime("%Y-%m-%d"))

    if plan:
        tasks = plan.get("tasks", [])
        tasks_text = "\n".join([f"• {task.get('task', task) if isinstance(task, dict) else task}" for task in tasks]) if tasks else "لا توجد مهام"

        await update.message.reply_text(
            ARABIC_RESPONSES["daily_plan"].format(
                tasks=tasks_text,
                review_time=plan.get("review_time", "غير محدد"),
                break_time=plan.get("break_time", "غير محدد")
            ),
            parse_mode="Markdown"
        )
    else:
        keyboard = [[InlineKeyboardButton("📅 إنشاء خطة", callback_data="plan_new")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "لا توجد خطة لليوم.",
            reply_markup=reply_markup
        )


async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to goals"""
    user_id = update.effective_user.id
    services = get_services(context)
    goals = services.goals.list(user_id)

    if not goals:
        keyboard = [[InlineKeyboardButton("➕ إضافة هدف", callback_data="goal_new")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("لا توجد أهداف حالياً.", reply_markup=reply_markup)
        return

    goals_text = "🎯 **أهدافك:**\n\n"
    for i, goal in enumerate(goals, 1):
        status_emoji = "✅" if goal["status"] == "completed" else "🔄"
        progress_bar = "█" * (goal["progress"] // 10) + "░" * (10 - goal["progress"] // 10)
        goals_text += f"{i}. {status_emoji} {goal['title']}\n"
        goals_text += f"   {progress_bar} {goal['progress']}%\n\n"

    await update.message.reply_text(goals_text, parse_mode="Markdown")


async def habits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to habits"""
    user_id = update.effective_user.id
    services = get_services(context)
    habits = services.habits.list(user_id)

    if not habits:
        keyboard = [[InlineKeyboardButton("➕ إضافة عادة", callback_data="habit_new")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("لا توجد عادات حالياً.", reply_markup=reply_markup)
        return

    habits_text = "🔄 **عاداتك:**\n\n"
    for i, habit in enumerate(habits, 1):
        streak = habit.get("current_streak", 0)
        fire = "🔥" if streak > 0 else "⚪"
        habits_text += f"{i}. {fire} {habit['name']} ({streak} يوم)\n"

    await update.message.reply_text(habits_text, parse_mode="Markdown")


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to progress"""
    user_id = update.effective_user.id
    services = get_services(context)
    summary = services.progress.weekly_summary(user_id)

    await update.message.reply_text(
        f"📈 **تقدمك (آخر 7 أيام)**\n\n"
        f"⏰ ساعات الدراسة: {summary['total_hours']}\n"
        f"✅ المهام المنجزة: {summary['total_tasks']}\n"
        f"📅 الأيام المسجلة: {summary['days']}",
        parse_mode="Markdown"
    )


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to daily review"""
    keyboard = [
        [InlineKeyboardButton("🌙 بدء المحاسبة", callback_data="review_daily")],
        [InlineKeyboardButton("📋 المراجعات السابقة", callback_data="review_history")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🌙 **المحاسبة اليومية**\n\n"
        "اختر ما تريد:",
        reply_markup=reply_markup
    )


async def coach_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to AI coach"""
    user_id = update.effective_user.id
    services = get_services(context)
    stats = services.dashboard.stats(user_id)

    await update.message.reply_text("⏳ جاري تحليل أدائك...")

    memory_block = services.memory.memory_block(user_id, subject="training")
    advice = await services.coaching.advice(stats, user_id=user_id, memory=memory_block)
    await update.message.reply_text(advice, parse_mode="Markdown")


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to dashboard"""
    user_id = update.effective_user.id
    services = get_services(context)
    stats = services.dashboard.stats(user_id)

    goals_progress = 0
    if stats["total_goals"] > 0:
        goals_progress = (stats["completed_goals"] / stats["total_goals"]) * 100

    dashboard_text = (
        f"📊 **لوحة التحكم**\n\n"
        f"🎯 **الأهداف:**\n"
        f"   إجمالي: {stats['total_goals']}\n"
        f"   مكتملة: {stats['completed_goals']}\n"
        f"   التقدم: {goals_progress:.0f}%\n\n"
        f"🔄 **العادات:**\n"
        f"   إجمالي: {stats['total_habits']}\n"
        f"   سلسلة: {stats['total_streaks']} يوم\n\n"
        f"📈 **هذا الأسبوع:**\n"
        f"   ساعات دراسة: {stats['weekly_study_hours']}\n"
        f"   مهام منجزة: {stats['weekly_tasks']}\n\n"
        f"📝 **المراجعات:** {stats['total_reviews']}"
    )

    await update.message.reply_text(dashboard_text, parse_mode="Markdown")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to settings"""
    keyboard = [
        [InlineKeyboardButton("👤 تعديل الملف الشخصي", callback_data="settings_profile")],
        [InlineKeyboardButton("🔔 إعدادات التذكيرات", callback_data="settings_reminders")],
        [InlineKeyboardButton("🤖 حالة الذكاء الاصطناعي", callback_data="settings_ai")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚙️ **الإعدادات**\n\n"
        "اختر ما تريد تعديله:",
        reply_markup=reply_markup
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current conversation"""
    await update.message.reply_text("❌ تم إلغاء العملية.")
    return ConversationHandler.END


async def knowledge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick access to the knowledge library menu."""
    services = get_services(context)
    text, markup = _knowledge_menu_content_for(
        services.knowledge.counts(update.effective_user.id)
    )
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors - map domain errors to safe user messages."""
    logger.error(f"Update {update} caused error: {context.error}", exc_info=context.error)

    message = public_message(context.error)

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(message)
        elif update and update.callback_query:
            await update.callback_query.answer(message, show_alert=True)
    except Exception as e:  # noqa: BLE001 - the handler itself must never crash
        logger.error(f"Error in error handler: {e}")


async def scheduler_post_init(app: Application) -> None:
    """Create and start the reminder scheduler when the bot boots."""
    scheduler = ReminderScheduler(app)
    app.bot_data["reminder_scheduler"] = scheduler
    await scheduler.start()


async def scheduler_post_stop(app: Application) -> None:
    """Stop the reminder scheduler when the bot shuts down."""
    scheduler = app.bot_data.get("reminder_scheduler")
    if scheduler is not None:
        scheduler.stop()


def build_application() -> Application:
    """Build the fully-wired bot application."""
    application = (
        Application.builder()
        .token(settings.bot_token)
        .job_queue(JobQueue())
        .post_init(scheduler_post_init)
        .post_stop(scheduler_post_stop)
        .build()
    )

    # Attach the DI container so handlers resolve services from context.
    application.bot_data.update(build_container().bot_data())

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("plan", plan_command))
    application.add_handler(CommandHandler("goals", goals_command))
    application.add_handler(CommandHandler("habits", habits_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("coach", coach_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("knowledge", knowledge_command))

    # Add conversation handlers
    application.add_handler(get_registration_handlers())
    application.add_handler(get_learning_handlers())
    application.add_handler(get_goal_handlers())
    application.add_handler(get_plan_handlers())
    application.add_handler(get_study_handlers())
    application.add_handler(get_english_handlers())
    application.add_handler(get_programming_handlers())
    application.add_handler(get_accountability_handlers())
    application.add_handler(get_habit_handlers())
    application.add_handler(get_reminder_handlers())

    # Add callback query handlers (menu handlers)
    for handler in get_menu_handlers():
        application.add_handler(handler)

    # Add message handlers for the knowledge engine (documents + ask mode).
    application.add_handler(MessageHandler(filters.Document.ALL, knowledge_document))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, knowledge_question)
    )
    # Add the memory engine's text-mode handler (search / edit / forget).
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, memory_text)
    )

    # Add error handler
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    """Main function to start the bot"""
    logger.info("Starting AUSTRO AI Bot...")
    logger.info("=" * 50)
    logger.info("AI Engine: Google Gemini Flash 2.5 (FREE)")
    logger.info("Cost: 1500 requests/day FREE")
    logger.info("=" * 50)

    # Build and run the application
    application = build_application()

    logger.info("All handlers loaded successfully!")
    logger.info("Bot is running! Press Ctrl+C to stop.")
    logger.info("=" * 50)

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


__all__ = [
    "build_application",
    "main",
    "scheduler_post_init",
    "scheduler_post_stop",
]