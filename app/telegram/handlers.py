"""
AUSTRO AI - Telegram handlers (PRESENTATION ONLY).

Every callback here is a thin adapter: it reads Telegram updates, calls the
right application service via the DI container (`get_services(context)`) and
renders the reply. No business rules, no SQL, no direct AI calls live here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config.prompts import ARABIC_RESPONSES
from app.core.container import get_services
from app.core.errors import ValidationError
from app.domain.entities import DailyReviewData, NewGoal, NewHabit, NewPlan

import io
import json

logger = logging.getLogger(__name__)


async def fallback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Common async cancel handler for ConversationHandler fallbacks."""
    if update and getattr(update, "message", None):
        await update.message.reply_text("تم الإلغاء")
    elif update and getattr(update, "callback_query", None):
        await update.callback_query.answer("تم الإلغاء")
    return ConversationHandler.END


# Conversation states
(
    REG_AGE, REG_EDUCATION, REG_GOALS, REG_TIME, REG_CONFIRM,
    GOAL_TITLE, GOAL_DESC, GOAL_CATEGORY, GOAL_STAGES, GOAL_DEADLINE,
    PLAN_TASKS, PLAN_PRIORITIES, PLAN_REVIEW_TIME, PLAN_BREAK_TIME,
    HABIT_NAME, HABIT_DESC, HABIT_FREQUENCY, HABIT_TIME,
    STUDY_SUBJECT, STUDY_TOPIC, STUDY_DURATION,
    ENGLISH_TEXT, ENGLISH_LEVEL,
    CODE_REVIEW, CODE_LANGUAGE,
    REVIEW_ACCOMPLISHED, REVIEW_LEARNED, REVIEW_OBSTACLES, REVIEW_TOMORROW, REVIEW_MOOD,
    REMINDER_TITLE, REMINDER_MESSAGE, REMINDER_TIME, REMINDER_CONFIRM,
) = range(34)

# Adaptive learning hub conversation states (kept above the range to avoid
# shifting the existing conversation state numbering).
LEARN_GOAL_TITLE = 100
LEARN_GOAL_OBJECTIVES = 101


# ==================== REGISTRATION HANDLERS ====================

async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start user registration."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📝 **التسجيل - الخطوة 1/5**\n\nما هو عمرك؟",
        parse_mode="Markdown",
    )
    return REG_AGE


async def reg_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle age input (validated by the user service)."""
    services = get_services(context)
    context.user_data["age"] = services.users.parse_age(update.message.text)

    keyboard = [
        [InlineKeyboardButton("🎓 ثانوي", callback_data="edu_highschool")],
        [InlineKeyboardButton("🎓 جامعي", callback_data="edu_university")],
        [InlineKeyboardButton("🎓 دراسات عليا", callback_data="edu_graduate")],
        [InlineKeyboardButton("👨‍💻 ذاتي", callback_data="edu_self")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📝 **التسجيل - الخطوة 2/5**\n\nما هو مستواك التعليمي؟",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return REG_EDUCATION


async def reg_education(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle education selection."""
    query = update.callback_query
    await query.answer()

    education_map = {
        "edu_highschool": "ثانوي",
        "edu_university": "جامعي",
        "edu_graduate": "دراسات عليا",
        "edu_self": "ذاتي",
    }

    context.user_data["education"] = education_map.get(query.data, "غير محدد")

    await query.edit_message_text(
        "📝 **التسجيل - الخطوة 3/5**\n\nما هي أهدافك الرئيسية؟ (اكتبها مفصولة بفواصل)\n"
        "مثال: تعلم البرمجة, تحسين الإنجليزية, بناء عادات صحية"
    )
    return REG_GOALS


async def reg_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goals input."""
    goals = [g.strip() for g in update.message.text.split(",")]
    context.user_data["goals"] = goals

    await update.message.reply_text(
        "📝 **التسجيل - الخطوة 4/5**\n\nكم ساعة متاحة لديك يومياً للدراسة/التعلم؟\n"
        "مثال: 2-3 ساعات"
    )
    return REG_TIME


async def reg_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle available time input and save the profile."""
    services = get_services(context)
    context.user_data["available_time"] = update.message.text

    user = update.effective_user
    services.users.update_profile(
        user_id=user.id,
        age=context.user_data.get("age"),
        education_level=context.user_data.get("education"),
        goals=context.user_data.get("goals"),
        daily_available_time=context.user_data.get("available_time"),
    )

    keyboard = [[InlineKeyboardButton("🚀 ابدأ الآن", callback_data="menu_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        ARABIC_RESPONSES["profile_created"],
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ==================== GOAL HANDLERS ====================

async def menu_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show goals menu."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    goals = services.goals.list(user_id)

    keyboard = [
        [InlineKeyboardButton("➕ هدف جديد", callback_data="goal_new")],
        [InlineKeyboardButton("📋 أهدافي الحالية", callback_data="goal_list")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    goals_text = "لا توجد أهداف حالياً." if not goals else f"لديك {len(goals)} هدف/أهداف."

    await query.edit_message_text(
        f"🎯 **أهدافك**\n\n{goals_text}",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def goal_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a new goal."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🎯 **هدف جديد - الخطوة 1/5**\n\nما هو عنوان هدفك؟"
    )
    return GOAL_TITLE


async def goal_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goal title."""
    context.user_data["goal_title"] = update.message.text
    await update.message.reply_text(
        "🎯 **هدف جديد - الخطوة 2/5**\n\nصف هدفك باختصار:"
    )
    return GOAL_DESC


async def goal_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goal description."""
    context.user_data["goal_desc"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("📚 دراسة", callback_data="cat_study")],
        [InlineKeyboardButton("💻 برمجة", callback_data="cat_programming")],
        [InlineKeyboardButton("🇬🇧 إنجليزي", callback_data="cat_english")],
        [InlineKeyboardButton("🔒 أمن سيبراني", callback_data="cat_cyber")],
        [InlineKeyboardButton("🎯 شخصي", callback_data="cat_personal")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎯 **هدف جديد - الخطوة 3/5**\n\nاختر تصنيف الهدف:",
        reply_markup=reply_markup,
    )
    return GOAL_CATEGORY


async def goal_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goal category."""
    query = update.callback_query
    await query.answer()

    category_map = {
        "cat_study": "دراسة",
        "cat_programming": "برمجة",
        "cat_english": "إنجليزي",
        "cat_cyber": "أمن سيبراني",
        "cat_personal": "شخصي",
    }

    context.user_data["goal_category"] = category_map.get(query.data, "عام")

    await query.edit_message_text(
        "🎯 **هدف جديد - الخطوة 4/5**\n\nما هي المراحل/الخطوات لتحقيق هذا الهدف؟ "
        "(مفصولة بفواصل)\nمثال: تعلم الأساسيات, التطبيق العملي, بناء مشروع"
    )
    return GOAL_STAGES


async def goal_stages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goal stages."""
    stages = [s.strip() for s in update.message.text.split(",")]
    context.user_data["goal_stages"] = stages

    await update.message.reply_text(
        "🎯 **هدف جديد - الخطوة 5/5**\n\nما هو الموعد النهائي للهدف؟ "
        "(YYYY-MM-DD أو اكتب 'لا يوجد')"
    )
    return GOAL_DEADLINE


async def goal_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle goal deadline and save."""
    services = get_services(context)
    deadline = update.message.text
    if deadline.lower() in ["لا يوجد", "none", "no"]:
        deadline = None

    user_id = update.effective_user.id
    goal_id = services.goals.create(NewGoal(
        user_id=user_id,
        title=context.user_data.get("goal_title"),
        description=context.user_data.get("goal_desc"),
        category=context.user_data.get("goal_category"),
        stages=context.user_data.get("goal_stages", []),
        deadline=deadline,
    ))

    keyboard = [[InlineKeyboardButton("🔙 رجوع للأهداف", callback_data="menu_goals")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if goal_id:
        await update.message.reply_text(
            "✅ **تم إنشاء الهدف بنجاح!**\n\n"
            f"🎯 {context.user_data.get('goal_title')}\n"
            f"📂 {context.user_data.get('goal_category')}",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ حدث خطأ أثناء إنشاء الهدف. حاول مرة أخرى.",
            reply_markup=reply_markup,
        )

    return ConversationHandler.END


async def goal_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of goals."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    goals = services.goals.list(user_id)

    if not goals:
        keyboard = [[InlineKeyboardButton("➕ إضافة هدف", callback_data="goal_new")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "لا توجد أهداف حالياً.",
            reply_markup=reply_markup,
        )
        return

    goals_text = "🎯 **أهدافك:**\n\n"
    for i, goal in enumerate(goals, 1):
        status_emoji = "✅" if goal["status"] == "completed" else "🔄"
        progress_bar = "█" * (goal["progress"] // 10) + "░" * (10 - goal["progress"] // 10)
        goals_text += f"{i}. {status_emoji} {goal['title']}\n"
        goals_text += f"   {progress_bar} {goal['progress']}%\n\n"

    keyboard = [
        [InlineKeyboardButton("➕ هدف جديد", callback_data="goal_new")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        goals_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== PLAN HANDLERS ====================

async def menu_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show plan menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📅 خطة اليوم", callback_data="plan_today")],
        [InlineKeyboardButton("📅 خطة جديدة", callback_data="plan_new")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📅 **الخطة اليومية**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def plan_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's plan."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    plan = services.plans.for_date(user_id, today)

    if plan:
        tasks = plan.get("tasks", [])

        def _format_task(task):
            if isinstance(task, dict):
                return task.get("task") or str(task)
            return str(task)

        tasks_text = "\n".join([f"• {_format_task(task)}" for task in tasks]) if tasks else "لا توجد مهام"

        keyboard = [
            [InlineKeyboardButton("✅ أكملت مهمة", callback_data="plan_complete")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_plan")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            ARABIC_RESPONSES["daily_plan"].format(
                tasks=tasks_text,
                review_time=plan.get("review_time", "غير محدد"),
                break_time=plan.get("break_time", "غير محدد"),
            ),
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        keyboard = [
            [InlineKeyboardButton("📅 إنشاء خطة", callback_data="plan_new")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_plan")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "لا توجد خطة لليوم. أنشئ واحدة!",
            reply_markup=reply_markup,
        )


async def plan_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a new plan."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📅 **خطة جديدة - الخطوة 1/4**\n\nما هي المهام التي تريد إنجازها اليوم؟ "
        "(مفصولة بفواصل)"
    )
    return PLAN_TASKS


async def plan_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plan tasks."""
    tasks = [{"task": t.strip(), "done": False} for t in update.message.text.split(",")]
    context.user_data["plan_tasks"] = tasks

    await update.message.reply_text(
        "📅 **خطة جديدة - الخطوة 2/4**\n\nما هي أولوياتك؟ (مفصولة بفواصل)"
    )
    return PLAN_PRIORITIES


async def plan_priorities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plan priorities."""
    priorities = [p.strip() for p in update.message.text.split(",")]
    context.user_data["plan_priorities"] = priorities

    await update.message.reply_text(
        "📅 **خطة جديدة - الخطوة 3/4**\n\nمتى وقت المراجعة؟ (مثال: 20:00)"
    )
    return PLAN_REVIEW_TIME


async def plan_review_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle review time."""
    context.user_data["plan_review_time"] = update.message.text

    await update.message.reply_text(
        "📅 **خطة جديدة - الخطوة 4/4**\n\nمتى وقت الراحة بين المهام؟ (مثال: 10 دقائق)"
    )
    return PLAN_BREAK_TIME


async def plan_break_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle break time and save the plan."""
    services = get_services(context)
    context.user_data["plan_break_time"] = update.message.text

    user_id = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")

    success = services.plans.create(NewPlan(
        user_id=user_id,
        date=today,
        tasks=context.user_data.get("plan_tasks", []),
        priorities=context.user_data.get("plan_priorities", []),
        review_time=context.user_data.get("plan_review_time"),
        break_time=context.user_data.get("plan_break_time"),
    ))

    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_plan")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if success:
        await update.message.reply_text(
            "✅ **تم إنشاء الخطة بنجاح!**\n\nلنبدأ العمل! 💪",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ حدث خطأ. حاول مرة أخرى.",
            reply_markup=reply_markup,
        )

    return ConversationHandler.END


# ==================== STUDY HANDLERS ====================

async def menu_study(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show study menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📖 شرح مفهوم", callback_data="study_explain")],
        [InlineKeyboardButton("📝 اختبار", callback_data="study_test")],
        [InlineKeyboardButton("📚 جلسة دراسة", callback_data="study_session")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📚 **وضع الدراسة**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def study_explain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start concept explanation."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📖 **شرح مفهوم**\n\nما هو المفهوم الذي تريد شرحه؟"
    )
    return STUDY_SUBJECT


async def study_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subject input."""
    context.user_data["study_subject"] = update.message.text

    await update.message.reply_text(
        "📖 **شرح مفهوم**\n\nفي أي مجال/مادة؟"
    )
    return STUDY_TOPIC


async def study_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic and generate the explanation via the learning service."""
    services = get_services(context)
    concept = context.user_data.get("study_subject")
    subject = update.message.text

    await update.message.reply_text("⏳ جاري توليد الشرح...")

    explanation = await services.learning.explain_concept(concept, subject)

    keyboard = [
        [InlineKeyboardButton("📝 اختبارني", callback_data="study_test")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_study")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        explanation,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def study_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a test."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📝 **اختبار**\n\nما هو الموضوع الذي تريد اختباره؟"
    )
    return STUDY_DURATION  # Reuse state


async def study_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle test topic and generate the test via the learning service."""
    services = get_services(context)
    topic = update.message.text

    await update.message.reply_text("⏳ جاري توليد الاختبار...")

    test = await services.learning.generate_test(topic)

    keyboard = [
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_study")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        test,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ==================== ENGLISH HANDLERS ====================

async def menu_english(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show English menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📚 درس يومي", callback_data="english_lesson")],
        [InlineKeyboardButton("✏️ تصحيح نص", callback_data="english_correct")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🇬🇧 **وضع الإنجليزية**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def english_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate an English lesson via the learning service."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("⏳ جاري توليد الدرس...")

    lesson = await services.learning.english_lesson()

    keyboard = [
        [InlineKeyboardButton("📚 درس آخر", callback_data="english_lesson")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_english")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        lesson,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def english_correct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start English correction."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "✏️ **تصحيح إنجليزي**\n\nأرسل النص الذي تريد تصحيحه:"
    )
    return ENGLISH_TEXT


async def english_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle English text correction via the learning service."""
    services = get_services(context)
    text = update.message.text

    await update.message.reply_text("⏳ جاري التصحيح...")

    correction = await services.learning.correct_english(text)

    keyboard = [
        [InlineKeyboardButton("✏️ تصحيح آخر", callback_data="english_correct")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_english")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        correction,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ==================== PROGRAMMING HANDLERS ====================

async def menu_programming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show programming menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("💻 مراجعة كود", callback_data="prog_review")],
        [InlineKeyboardButton("📖 شرح مفهوم برمجي", callback_data="prog_concept")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "💻 **وضع البرمجة**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def prog_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start code review."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "💻 **مراجعة كود**\n\nما هي لغة البرمجة؟"
    )
    return CODE_LANGUAGE


async def code_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle language input."""
    context.user_data["code_language"] = update.message.text

    await update.message.reply_text(
        "💻 **مراجعة كود**\n\nأرسل الكود الذي تريد مراجعته:"
    )
    return CODE_REVIEW


async def code_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle code review via the learning service."""
    services = get_services(context)
    code = update.message.text
    language = context.user_data.get("code_language", "python")

    await update.message.reply_text("⏳ جاري مراجعة الكود...")

    review = await services.learning.review_code(code, language)

    keyboard = [
        [InlineKeyboardButton("💻 مراجعة أخرى", callback_data="prog_review")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_programming")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        review,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ==================== DISCIPLINE HANDLERS ====================

async def menu_discipline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show discipline menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⚡ فحص الانضباط", callback_data="discipline_check")],
        [InlineKeyboardButton("🎯 تحدي اليوم", callback_data="discipline_challenge")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "⚡ **وضع الانضباط**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def discipline_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show discipline check."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        ARABIC_RESPONSES["discipline_check"],
        parse_mode="Markdown",
    )


# ==================== HABIT HANDLERS ====================

async def menu_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show habits menu."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    habits = services.habits.list(user_id)

    keyboard = [
        [InlineKeyboardButton("➕ عادة جديدة", callback_data="habit_new")],
        [InlineKeyboardButton("📋 عاداتي", callback_data="habit_list")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    habits_text = f"لديك {len(habits)} عادة/عادات." if habits else "لا توجد عادات حالياً."

    await query.edit_message_text(
        f"🔄 **العادات**\n\n{habits_text}",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def habit_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a new habit."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔄 **عادة جديدة - الخطوة 1/4**\n\nما اسم العادة؟"
    )
    return HABIT_NAME


async def habit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle habit name."""
    context.user_data["habit_name"] = update.message.text

    await update.message.reply_text(
        "🔄 **عادة جديدة - الخطوة 2/4**\n\nصف العادة باختصار:"
    )
    return HABIT_DESC


async def habit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle habit description."""
    context.user_data["habit_desc"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("يومي", callback_data="freq_daily")],
        [InlineKeyboardButton("أسبوعي", callback_data="freq_weekly")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🔄 **عادة جديدة - الخطوة 3/4**\n\nما هي تكرار العادة؟",
        reply_markup=reply_markup,
    )
    return HABIT_FREQUENCY


async def habit_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle habit frequency."""
    query = update.callback_query
    await query.answer()

    freq_map = {"freq_daily": "daily", "freq_weekly": "weekly"}
    context.user_data["habit_frequency"] = freq_map.get(query.data, "daily")

    await query.edit_message_text(
        "🔄 **عادة جديدة - الخطوة 4/4**\n\nمتى تريد التذكير؟ (مثال: 08:00)"
    )
    return HABIT_TIME


async def habit_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle habit reminder time and save."""
    services = get_services(context)
    reminder_time = update.message.text

    user_id = update.effective_user.id
    habit_id = services.habits.create(NewHabit(
        user_id=user_id,
        name=context.user_data.get("habit_name"),
        description=context.user_data.get("habit_desc"),
        frequency=context.user_data.get("habit_frequency", "daily"),
        reminder_time=reminder_time,
    ))

    keyboard = [[InlineKeyboardButton("🔙 رجوع للعادات", callback_data="menu_habits")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if habit_id:
        await update.message.reply_text(
            f"✅ **تم إنشاء العادة بنجاح!**\n\n🔄 {context.user_data.get('habit_name')}\n"
            f"⏰ تذكير يومي الساعة {reminder_time}",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ حدث خطأ. حاول مرة أخرى.",
            reply_markup=reply_markup,
        )

    return ConversationHandler.END


async def habit_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show habits list."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    habits = services.habits.list(user_id)

    if not habits:
        keyboard = [[InlineKeyboardButton("➕ إضافة عادة", callback_data="habit_new")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "لا توجد عادات حالياً.",
            reply_markup=reply_markup,
        )
        return

    habits_text = "🔄 **عاداتك:**\n\n"
    for i, habit in enumerate(habits, 1):
        streak = habit.get("current_streak", 0)
        fire = "🔥" if streak > 0 else "⚪"
        habits_text += f"{i}. {fire} {habit['name']} ({streak} يوم)\n"

    keyboard = [
        [InlineKeyboardButton("➕ عادة جديدة", callback_data="habit_new")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        habits_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== PROGRESS HANDLERS ====================

async def menu_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show progress menu."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    summary = services.progress.weekly_summary(user_id)

    keyboard = [
        [InlineKeyboardButton("📊 إحصائيات مفصلة", callback_data="progress_stats")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📈 **تقدمك (آخر 7 أيام)**\n\n"
        f"⏰ ساعات الدراسة: {summary['total_hours']}\n"
        f"✅ المهام المنجزة: {summary['total_tasks']}\n"
        f"📅 الأيام المسجلة: {summary['days']}",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== COACH HANDLERS ====================

async def menu_coach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show coach menu."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    stats = services.dashboard.stats(user_id)

    await query.edit_message_text("⏳ جاري تحليل أدائك...")

    memory_block = services.memory.memory_block(user_id, subject="training")
    advice = await services.coaching.advice(
        stats, user_id=user_id, memory=memory_block
    )

    keyboard = [
        [InlineKeyboardButton("🧠 نصيحة أخرى", callback_data="menu_coach")],
        [InlineKeyboardButton("📊 تحليل الأداء", callback_data="coach_analysis")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        advice,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def coach_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show performance analysis."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    stats = services.dashboard.stats(user_id)

    await query.edit_message_text("⏳ جاري التحليل...")

    analysis = await services.coaching.analyze(stats)

    keyboard = [
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_coach")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        analysis,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== DASHBOARD HANDLERS ====================

async def menu_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show dashboard."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
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

    keyboard = [
        [InlineKeyboardButton("🔄 تحديث", callback_data="menu_dashboard")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        dashboard_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== ACCOUNTABILITY HANDLERS ====================

async def menu_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show review menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🌙 محاسبة يومية", callback_data="review_daily")],
        [InlineKeyboardButton("📋 مراجعات سابقة", callback_data="review_history")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🌙 **المحاسبة اليومية**\n\nاختر ما تريد:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def review_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start daily review conversation."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        ARABIC_RESPONSES["night_review_step_1"]
    )
    return REVIEW_ACCOMPLISHED


async def review_accomplished(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle accomplished input."""
    context.user_data["review_accomplished"] = update.message.text

    await update.message.reply_text(
        ARABIC_RESPONSES["night_review_step_2"]
    )
    return REVIEW_LEARNED


async def review_learned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learned input."""
    context.user_data["review_learned"] = update.message.text

    await update.message.reply_text(
        ARABIC_RESPONSES["night_review_step_3"]
    )
    return REVIEW_OBSTACLES


async def review_obstacles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle obstacles input."""
    context.user_data["review_obstacles"] = update.message.text

    await update.message.reply_text(
        ARABIC_RESPONSES["night_review_step_4"]
    )
    return REVIEW_TOMORROW


async def review_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tomorrow plan."""
    context.user_data["review_tomorrow"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("😊 جيد", callback_data="mood_4")],
        [InlineKeyboardButton("😐 عادي", callback_data="mood_3")],
        [InlineKeyboardButton("😔 صعب", callback_data="mood_2")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        ARABIC_RESPONSES["night_review_step_5"],
        reply_markup=reply_markup,
    )
    return REVIEW_MOOD


async def review_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mood and save the review."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    mood_map = {"mood_4": 4, "mood_3": 3, "mood_2": 2}
    mood = mood_map.get(query.data, 3)

    user_id = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")

    success = services.reviews.save(DailyReviewData(
        user_id=user_id,
        date=today,
        accomplished=context.user_data.get("review_accomplished", ""),
        learned=context.user_data.get("review_learned", ""),
        obstacles=context.user_data.get("review_obstacles", ""),
        tomorrow_plan=context.user_data.get("review_tomorrow", ""),
        mood=mood,
    ))

    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_review")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if success:
        await query.edit_message_text(
            "✅ **تم حفظ المحاسبة اليومية!**\n\n"
            "أحسنت على الالتزام! 💪",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ حدث خطأ. حاول مرة أخرى.",
            reply_markup=reply_markup,
        )

    return ConversationHandler.END


# ==================== SETTINGS HANDLERS ====================

async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("👤 تعديل الملف الشخصي", callback_data="settings_profile")],
        [InlineKeyboardButton("🔔 إعدادات التذكيرات", callback_data="settings_reminders")],
        [InlineKeyboardButton("🤖 حالة الذكاء الاصطناعي", callback_data="settings_ai")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "⚙️ **الإعدادات**\n\nاختر ما تريد تعديله:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def settings_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show profile settings."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = services.users.profile(user_id)

    if user:
        profile_text = (
            f"👤 **ملفك الشخصي**\n\n"
            f"الاسم: {user.get('first_name', 'غير محدد')}\n"
            f"العمر: {user.get('age', 'غير محدد')}\n"
            f"المستوى التعليمي: {user.get('education_level', 'غير محدد')}\n"
            f"الأهداف: {user.get('goals', 'غير محدد')}\n"
            f"الوقت المتاح: {user.get('daily_available_time', 'غير محدد')}\n"
            f"الوضع الحالي: {user.get('current_mode', 'عام')}\n\n"
            "هل تريد تعديل أي معلومة؟"
        )
    else:
        profile_text = "لم يتم العثور على ملفك الشخصي."

    keyboard = [
        [InlineKeyboardButton("🔄 إعادة التسجيل", callback_data="start_registration")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        profile_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def settings_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show reminder settings."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    reminders = services.reminders.active(user_id)

    reminders_text = "🔔 **تذكيراتك:**\n\n"
    if reminders:
        for i, rem in enumerate(reminders, 1):
            reminders_text += (
                f"{i}. {rem.get('title', 'بدون عنوان')} - "
                f"{rem.get('scheduled_time', 'غير محدد')}\n"
            )
    else:
        reminders_text += "لا توجد تذكيرات نشطة.\n\n"
        reminders_text += "التذكيرات الافتراضية:\n"
        reminders_text += "☀️ صباحية: 08:00\n"
        reminders_text += "🌞 ظهراً: 12:00\n"
        reminders_text += "🌆 مساءً: 18:00\n"
        reminders_text += "🌙 محاسبة: 21:00\n"

    keyboard = [
        [InlineKeyboardButton("➕ تذكير جديد", callback_data="reminder_new")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        reminders_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def reminder_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a new reminder."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔔 **تذكير جديد**\n\nما عنوان التذكير؟"
    )
    return REMINDER_TITLE


async def reminder_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder title."""
    context.user_data["reminder_title"] = update.message.text

    await update.message.reply_text(
        "🔔 **تذكير جديد**\n\nما رسالة التذكير؟"
    )
    return REMINDER_MESSAGE


async def reminder_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder message."""
    context.user_data["reminder_message"] = update.message.text

    await update.message.reply_text(
        "🔔 **تذكير جديد**\n\nمتى تريد التذكير؟ (HH:MM)"
    )
    return REMINDER_TIME


async def reminder_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder time."""
    context.user_data["reminder_time"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("✅ تأكيد", callback_data="reminder_confirm")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="menu_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🔔 **تأكيد التذكير**\n\n"
        f"العنوان: {context.user_data.get('reminder_title')}\n"
        f"الرسالة: {context.user_data.get('reminder_message')}\n"
        f"الوقت: {context.user_data.get('reminder_time')}\n\n"
        f"هل تريد الحفظ؟",
        reply_markup=reply_markup,
    )
    return REMINDER_CONFIRM


async def reminder_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and save the reminder."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    success = services.reminders.create_daily_reminder(
        user_id=user_id,
        title=context.user_data.get("reminder_title"),
        message=context.user_data.get("reminder_message"),
        time_str=context.user_data.get("reminder_time", "08:00"),
    )

    keyboard = [[InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if success:
        await query.edit_message_text(
            "✅ **تم إنشاء التذكير بنجاح!**\n\nسيتم إرسال التذكير في الوقت المحدد.",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ حدث خطأ. حاول مرة أخرى.",
            reply_markup=reply_markup,
        )

    return ConversationHandler.END


async def settings_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show AI status."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    status = services.chat.status()

    keyboard = [
        [InlineKeyboardButton("🔄 إعادة الاتصال", callback_data="settings_ai_reconnect")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🤖 **حالة الذكاء الاصطناعي**\n\n{status}",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def settings_ai_reconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reconnect to Gemini."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    status = await services.chat.reconnect()

    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🔄 **تم إعادة الاتصال**\n\n{status}",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==================== KNOWLEDGE ENGINE HANDLERS ====================

_STATUS_LABELS = {
    "pending": "⏳ بانتظار المعالجة",
    "processing": "⏳ قيد المعالجة",
    "completed": "✅ جاهز",
    "failed": "❌ فشل المعالجة",
    "cancelled": "🚫 ملغي",
}


def _knowledge_menu_content_for(counts: dict) -> tuple:
    text = (
        "📚 **مكتبة المعرفة**\n\n"
        f"📖 كتبك: {counts.get('total', 0)} "
        f"(جاهزة: {counts.get('ready', 0)})\n"
        f"⏳ قيد المعالجة: {counts.get('processing', 0)}\n"
        f"❌ فشلت: {counts.get('failed', 0)}\n\n"
        "اختر ما تريد:"
    )
    keyboard = [
        [InlineKeyboardButton("📚 كتبي", callback_data="knowledge_books")],
        [InlineKeyboardButton("➕ إضافة كتاب", callback_data="knowledge_upload")],
        [InlineKeyboardButton("🔍 ابحث في كتبك", callback_data="knowledge_search")],
        [InlineKeyboardButton("🗂️ المجموعات", callback_data="knowledge_collections")],
        [InlineKeyboardButton("⏳ قيد المعالجة", callback_data="knowledge_processing")],
        [InlineKeyboardButton("⚙️ إعدادات المعرفة", callback_data="knowledge_settings")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def menu_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the knowledge library menu."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    text, markup = _knowledge_menu_content_for(services.knowledge.counts(user_id))
    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def knowledge_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the user's books with their status."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    sources = services.knowledge.list_sources(user_id)

    if not sources:
        keyboard = [[InlineKeyboardButton("📥 إضافة كتاب", callback_data="knowledge_upload")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
        await query.edit_message_text(
            "📚 لا توجد كتب في مكتبتك بعد. أضف كتابك الأول!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    lines = ["📚 **كتبك:**\n"]
    for source in sources[:12]:
        label = _STATUS_LABELS.get(source["status"], source["status"])
        title = source.get("title") or source.get("file_name") or "بدون عنوان"
        lines.append(f"• {label} {title}")
    keyboard = [
        [InlineKeyboardButton("📥 إضافة كتاب", callback_data="knowledge_upload")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")],
    ]
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def knowledge_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explain how to add a book."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    max_mb = services.knowledge.settings_info()["max_file_size_mb"]
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
    hint = (
        "📥 **إضافة كتاب**\n\n"
        "أرسل لي ملف الكتاب مباشرة وسأعالجه تلقائياً.\n\n"
        "الصيغ المدعومة: **PDF**, **TXT**, **MD**, **DOCX**, **EPUB**\n"
        f"الحد الأقصى للحجم: {max_mb}MB\n\n"
        "بعد المعالجة سيعتبر الكتاب جاهزاً ويمكنك السؤال عنه."
    )
    await query.edit_message_text(
        hint, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def knowledge_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask a question about the user's books."""
    query = update.callback_query
    await query.answer()
    context.user_data["knowledge_mode"] = "ask"
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
    await query.edit_message_text(
        "🔍 **السؤال عن كتبك**\n\n"
        "اكتب سؤالك الآن وسأجيب عليه بالاعتماد على كتبك فقط.\n"
        "مثال: ما هي أهم طريقة لتنظيم الوقت حسب كتابي؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def knowledge_collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's collections."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    collections = services.knowledge.collections.list(user_id)
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
    if not collections:
        await query.edit_message_text(
            "🗂️ لا توجد مجموعات بعد.\n\n"
            "ستتوفر المجموعات في تحديث قادم لتنظيم كتبك.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    lines = ["🗂️ **مجموعاتك:**\n"]
    for collection in collections[:12]:
        lines.append(f"• {collection.name}")
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def knowledge_processing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show in-flight processing jobs."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    processing = services.knowledge.processing_sources(user_id)
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
    if not processing:
        await query.edit_message_text(
            "⏳ لا توجد كتب قيد المعالجة حالياً.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    lines = ["⏳ **قيد المعالجة:**\n"]
    for source in processing[:10]:
        state = source.get("ingestion_state") or ""
        title = source.get("title") or source.get("file_name") or "بدون عنوان"
        lines.append(f"• {title} ({state})")
    lines.append("\nسيتم إعلامك عندما تصبح جاهزة.")
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def knowledge_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show knowledge engine settings/limits."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    info = services.knowledge.settings_info()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_knowledge")]]
    text = (
        "⚙️ **إعدادات المعرفة**\n\n"
        f"📄 الحد الأقصى لحجم الملف: {info['max_file_size_mb']}MB\n"
        f"📖 الحد الأقصى للصفحات: {info['max_pages']}\n"
        f"🧩 حجم المقطع: {info['chunk_size']} (تداخل {info['chunk_overlap']})\n"
        f"🧠 نموذج التضمين: {info['embedding_model']} "
        f"v{info['embedding_version']} ({info['dimensions']})\n"
        f"🔍 نتائج البحث: {info['top_k']}"
    )
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def knowledge_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a book file uploaded by the user (presentation adapter)."""
    services = get_services(context)
    user_id = update.effective_user.id
    document = update.message.document
    if document is None:
        return
    file_name = document.file_name or "document.pdf"

    max_size = services.knowledge.settings_info()["max_file_size_mb"] * 1024 * 1024
    if (document.file_size or 0) > max_size:
        await update.message.reply_text(
            f"❌ حجم الملف يتجاوز الحد المسموح ({max_size // (1024*1024)}MB)."
        )
        return

    try:
        file = await context.bot.get_file(document.file_id)
        data = await file.download_as_bytearray()
    except Exception as e:  # noqa: BLE001 - download failures are surfaced safely
        logger.error(f"Knowledge download failed for user {user_id}: {e}")
        await update.message.reply_text("❌ تعذر تحميل الملف. حاول مجدداً.")
        return

    try:
        result = services.knowledge.register_upload(
            owner_user_id=user_id,
            file_name=file_name,
            data=bytes(data),
            mime_type=document.mime_type,
            original_ref=document.file_id,
        )
    except ValidationError as e:
        await update.message.reply_text(str(e))
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"Knowledge register failed for user {user_id}: {e}")
        await update.message.reply_text("❌ تعذر حفظ الكتاب. حاول مجدداً.")
        return

    if result.get("duplicate"):
        await update.message.reply_text("📚 هذا الكتاب موجود في مكتبتك بالفعل.")
        return

    source_id = result.get("source_id")
    chat_id = update.message.chat_id
    await update.message.reply_text(
        f"📥 تم استلام الكتاب **{file_name}**!\n"
        "بدأت المعالجة وسأعلمك عندما يصبح جاهزاً.",
        parse_mode="Markdown",
    )
    if context.application is not None:
        context.application.create_task(
            _ingest_book(context, user_id, source_id, chat_id)
        )


async def _ingest_book(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                       source_id: int, chat_id: int) -> None:
    """Background ingestion with progress notifications (runs off the main handler)."""
    services = get_services(context)

    async def notify(state: str, title: str, message: str) -> None:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"{title}\n{message}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Ingestion notification failed: {e}")

    try:
        result = await services.knowledge.process_source(user_id, source_id, progress=notify)
        if result.status == "completed":
            await context.bot.send_message(
                chat_id=chat_id,
                text="📚 ✅ كتابك أصبح جاهزاً! الآن يمكنك سؤالي عنه.",
            )
        else:
            detail = result.error or "حدث خطأ غير معروف"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ تعذرت معالجة الكتاب:\n{detail}",
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Background ingestion crashed for {source_id}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ حدث خطأ أثناء معالجة الكتاب. حاول إرساله مرة أخرى.",
            )
        except Exception:  # noqa: BLE001
            logger.error("Could not notify about ingestion crash")


async def knowledge_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer a question about the user's books (only in ask mode)."""
    if context.user_data.get("knowledge_mode") != "ask":
        return
    context.user_data["knowledge_mode"] = None

    services = get_services(context)
    user_id = update.effective_user.id
    question = update.message.text or ""
    await update.message.reply_text("🔍 جاري البحث في كتبك...")
    try:
        answer = await services.knowledge.answer(user_id, question)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Knowledge answer failed for user {user_id}: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء البحث. حاول مجدداً.")
        return

    text = answer.text
    if answer.citations:
        citations = "\n\n".join(c.formatted() for c in answer.citations)
        text = f"{text}\n\n{citations}"
    await update.message.reply_text(text, parse_mode="Markdown")


# ==================== MEMORY ENGINE HANDLERS ====================

_MEMORY_TYPE_LABELS = {
    "profile": "الملف الشخصي",
    "preference": "التفضيلات",
    "goal": "الأهداف",
    "habit": "العادات",
    "learning_state": "الحالة الدراسية",
    "skill": "المهارات",
    "weakness": "نقاط الضعف",
    "strength": "نقاط القوة",
    "routine": "الروتين اليومي",
    "communication_preference": "تفضيل التواصل",
    "coaching_preference": "تفضيل التدريب",
    "important_context": "سياق مهم",
    "episodic_event": "أحداث",
    "achievement": "إنجازات",
    "user_instruction": "تعليمات",
}


def _memory_type_label(memory_type: str) -> str:
    return _MEMORY_TYPE_LABELS.get(memory_type, memory_type)


def _memory_menu_content_for(counts: dict, enabled: bool, pending: int) -> tuple:
    state = "✅ مفعلة" if enabled else "⛔ متوقفة"
    lines = [
        "🧠 **ذاكرتي الشخصية**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الذاكرة التلقائية: {state}",
        f"الذكريات المحفوظة: {counts.get('total', 0)}",
    ]
    if pending:
        lines.append(f"⚠️ تنتظر تأكيدك: {pending}")
    if not enabled:
        lines.append("\n_الذاكرة متوقفة:\nأخبرني بأي شيء تريد أن أتذكره\n"
                     "عنك وسأحفظه بذكاء._")
    lines.append("\nاختر ما تريد:")
    keyboard = [
        [InlineKeyboardButton("👁️ عرض الذاكرة", callback_data="memory_view")],
        [InlineKeyboardButton("🔍 بحث", callback_data="memory_search")],
        [InlineKeyboardButton("✏️ تعديل", callback_data="memory_edit"),
         InlineKeyboardButton("🗑️ نسيان", callback_data="memory_forget")],
        [InlineKeyboardButton("⚙️ إعدادات", callback_data="memory_settings"),
         InlineKeyboardButton("📤 تصدير", callback_data="memory_export")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def menu_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the personal memory menu."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    counts = services.memory.counts(user_id)
    enabled = services.memory.enabled(user_id)
    pending = len(services.memory.pending(user_id))
    text, markup = _memory_menu_content_for(counts, enabled, pending)
    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


def _format_memory_line(title: str, items: list, limit: int = 12) -> str:
    if not items:
        return f"{title}\nلا توجد ذكريات بعد.\n"
    lines = [f"{title}\n"]
    for item in items[:limit]:
        label = _memory_type_label(item["memory_type"])
        claim = (item.get("claim") or "")[:80]
        conf = "⚠️" if item.get("confidence") == "low" else "✅"
        lines.append(
            f"{conf} #{item['memory_id']} [{label}] "
            f"{item.get('subject') or ''}: {claim}"
        )
    if len(items) > limit:
        lines.append(f"\n... و{len(items) - limit} أخرى")
    return "\n".join(lines)


async def memory_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the user's saved memories (newest first)."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    items = services.memory.list(user_id)
    keyboard = [
        [InlineKeyboardButton("🔍 بحث", callback_data="memory_search")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")],
    ]
    await query.edit_message_text(
        _format_memory_line("👁️ **ما أتذكره عنك:**", items),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def memory_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["memory_mode"] = "search"
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]]
    await query.edit_message_text(
        "🔍 **البحث في الذاكرة**\n\n"
        "اكتب كلمة أو موضوعاً ليبحث عنه من ذكرياتك.\n"
        "مثال: Write \"لغة برمجة\" للبحث عنها.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def memory_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["memory_mode"] = "edit"
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]]
    await query.edit_message_text(
        "✏️ **تعديل ذاكرة**\n\n"
        "أرسل رقم # الذاكرة ثم النص الجديد.\n"
        "مثال: 3 أحب القهوة العربية فقط\n\n"
        "(أرقام الذاكرة تظهر في عرض الذاكرة)",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def memory_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["memory_mode"] = "forget"
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]]
    await query.edit_message_text(
        "🗑️ **نسيان ذاكرة**\n\n"
        "أرسل رقم # الذاكرة لحذفها من ذاكرتي.\n"
        "أو اكتب «all» لنسيان كل الذكريات مرة واحدة.\n\n"
        "(أرقام الذاكرة تظهر في عرض الذاكرة)",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def memory_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("✅ نعم، انسَ كل شيء", callback_data="memory_clear_confirm")],
        [InlineKeyboardButton("🔙 تراجع", callback_data="memory_settings")],
    ]
    await query.edit_message_text(
        "🧹 **مسح الذاكرة**\n\n"
        "هل تريد نسيان كل الذكريات المحفوظة عنك؟\n"
        "(لا يمكن التراجع عن هذا الإجراء)",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def memory_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    count = services.memory.clear(user_id)
    text, markup = _memory_menu_content_for(
        {"total": 0}, services.memory.enabled(user_id), 0
    )
    await query.edit_message_text(
        f"🧹 تم نسيان {count} ذاكرة.\n\n{text}",
        reply_markup=markup,
        parse_mode="Markdown",
    )


async def memory_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    enabled = services.memory.enabled(user_id)
    body = [
        "⚙️ **إعدادات الذاكرة**\n",
        "❓ **الذاكرة التلقائية:**",
        "أتذكر تلقائياً ما تخبرني به عن نفسك، أهدافك،\n"
        "تفضيلاتك وعاداتك لأساعدك بشكل شخصي.",
    ]
    toggle_label = "⛔ إيقاف الذاكرة التلقائية" if enabled else "✅ تفعيل الذاكرة التلقائية"
    pending = services.memory.pending(user_id)
    keyboard = [
        [InlineKeyboardButton(toggle_label, callback_data="memory_toggle")],
    ]
    if pending:
        keyboard.append(
            [InlineKeyboardButton(
                f"🔔 طلبات تأكيد ({len(pending)})", callback_data="memory_pending")]
        )
    if enabled:
        keyboard.append(
            [InlineKeyboardButton("🧹 مسح كل الذكريات", callback_data="memory_clear")]
        )
    keyboard.append(
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]
    )
    await query.edit_message_text(
        "\n".join(body), reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def memory_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    enabled = not services.memory.enabled(user_id)
    services.memory.set_auto_enabled(user_id, enabled)
    status = "✅ الذاكرة التلقائية مفعلة الآن." if enabled else \
        "⛔ الذاكرة التلقائية متوقفة الآن."
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="memory_settings")]]
    # Also try to write memory while it is disabled -> show a demo hint.
    hint = (
        "\nعندما تكون الذاكرة متوقفة لن أتذكر شيئاً "
        "إلا إذا قلت «تذكر هذا» بصراحة."
    ) if not enabled else "\nسأستمع وأتذكر كل ما يتعلق بك."
    await query.edit_message_text(
        f"{status}{hint}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def memory_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    pending = services.memory.pending(user_id)
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="memory_settings")]]
    if not pending:
        await query.edit_message_text(
            "🔔 لا توجد طلبات تأكيد حالياً.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    lines = ["🔔 **ذهكريات تحتاج موافقتك:**\n"]
    rows = []
    for item in pending:
        label = _memory_type_label(item["memory_type"])
        lines.append(
            f"#{item['memory_id']} [{label}] "
            f"{item.get('subject') or ''}: {(item.get('claim') or '')[:80]}"
        )
        rows.append([
            InlineKeyboardButton("✅ حفظ", callback_data=f"memory_accept_{item['memory_id']}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"memory_reject_{item['memory_id']}"),
        ])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="memory_settings")])
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )


async def memory_consent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept or reject a pending memory (callback like memory_accept_5)."""
    query = update.callback_query
    await query.answer()
    callback = query.data or ""
    _, action, raw_id = callback.split("_", 2)
    memory_id = int(raw_id)
    services = get_services(context)
    user_id = update.effective_user.id
    ok = services.memory.confirm(user_id, memory_id, accept=(action == "accept"))
    text = "✅ تم الحفظ في ذاكرتي." if ok and action == "accept" else \
        "❌ تم تجاهل الذاكرة."
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="memory_pending")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def memory_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export all memories as a JSON document."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    payload = services.memory.export(user_id)
    if payload is None:
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]]
        await query.edit_message_text(
            "📤 لا توجد ذكريات لتصديرها بعد.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    buffer = io.BytesIO(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    )
    buffer.seek(0)
    await query.edit_message_text("📤 **تصدير الذاكرة**\nجارٍ تجهيز الملف...")
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(buffer, filename="austro_memory.json"),
        caption=f"🧠 نسخة من ذاكرتك ({payload['memory_count']} ذكريات)",
    )


async def memory_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle memory-mode text (search / edit / forget)."""
    mode = context.user_data.get("memory_mode")
    if not mode:
        return
    services = get_services(context)
    user_id = update.effective_user.id
    text = update.message.text or ""
    back = [InlineKeyboardButton("🔙 رجوع", callback_data="menu_memory")]

    if mode == "search":
        context.user_data["memory_mode"] = None
        results = services.memory.search(user_id, text)
        await update.message.reply_text(
            _format_memory_line(f"🔍 **نتائج البحث عن: {text}**:", results),
            reply_markup=InlineKeyboardMarkup(back),
            parse_mode="Markdown",
        )
        return

    if mode == "edit":
        first, _, rest = text.strip().partition(" ")
        if not first.isdigit() or not rest.strip():
            await update.message.reply_text(
                "اكتب رقم الذاكرة ثم النص الجديد، مثل: 3 أحب الشاي بالنعناع",
                reply_markup=InlineKeyboardMarkup(back),
            )
            return
        context.user_data["memory_mode"] = None
        ok = services.memory.edit(user_id, int(first), rest.strip())
        reply = "✅ تم تعديل الذاكرة." if ok else \
            "❌ لم أجد ذاكرة بهذا الرقم."
        await update.message.reply_text(reply, reply_markup=InlineKeyboardMarkup(back))
        return

    if mode == "forget":
        if text.strip().lower() in ("all", "الكل", "انسى الكل"):
            count = services.memory.clear(user_id)
            context.user_data["memory_mode"] = None
            await update.message.reply_text(
                f"🧹 تم نسيان {count} ذاكرة.",
                reply_markup=InlineKeyboardMarkup(back),
            )
            return
        if not text.strip().isdigit():
            await update.message.reply_text(
                "أرسل رقم الذاكرة فقط، أو «all» لنسيان الكل.",
                reply_markup=InlineKeyboardMarkup(back),
            )
            return
        context.user_data["memory_mode"] = None
        memory_id = int(text.strip())
        ok = services.memory.forget(user_id, memory_id)
        await update.message.reply_text(
            "🗑️ تم نسيان الذاكرة." if ok else "❌ لم أجد ذاكرة بهذا الرقم.",
            reply_markup=InlineKeyboardMarkup(back),
        )


# ==================== MAIN MENU HANDLER ====================

async def menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user

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

    await query.edit_message_text(
        f"مرحباً بعودتك {user.first_name}! 👋\n\n"
        f"أنا AUSTRO AI، جاهز لمساعدتك اليوم! 💪\n\n"
        f"اختر ما تريد:",
        reply_markup=reply_markup,
    )


# ==================== ADAPTIVE LEARNING HUB (PHASE E) ====================


async def menu_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Learning hub: overview + actions."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id

    line = "🧠 **رحلة التعلم**\n━━━━━━━━━━━━━━━━━━━━\n"
    goals = services.learning_engine.goals(user_id)
    long_term = [g for g in goals if g.get("kind") == "long_term"]
    curriculum_id = None
    curriculum_goal = None
    if long_term:
        goal_id = long_term[0]["goal_id"]
        curricula = services.learning_engine.curricula(user_id, goal_id)
        curriculum_goal = long_term[0]
        curriculum_id = curricula[0]["curriculum_id"] if curricula else None

    if curriculum_id is None:
        line += "لا يوجد مسار تعلم بعد.\nلك ابدأ بإنشاء هدف وماذا تريد أن تتعلم."
        keyboard = [
            [InlineKeyboardButton("🎯 هدف تعلم جديد", callback_data="learn_newgoal")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(line, reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
        return

    focus = services.learning_engine.coach_today(user_id)
    overview = services.learning_engine.progress_overview(user_id)
    line += f"🎯 الهدف: **{curriculum_goal.get('title')}**\n"
    line += (f"📚 أهداف تعليمية: {overview.objectives_total} — متقن "
             f"{overview.mastered} | قريب {overview.near_mastery}\n")
    line += f"🔁 مراجعات مستحقة: {overview.due_reviews}\n\n"
    line += f"💡 {focus.get('reason') or focus.get('focus')}"
    keyboard = [
        [InlineKeyboardButton("📖 الدرس التالي", callback_data="menu_learn_next")],
        [InlineKeyboardButton("📝 اختبار سريع", callback_data="menu_learn_quiz"),
         InlineKeyboardButton("🔁 المراجعة", callback_data="menu_learn_review")],
        [InlineKeyboardButton("📋 خطة اليوم", callback_data="menu_learn_plan"),
         InlineKeyboardButton("📊 ملخص الأسبوع", callback_data="menu_learn_week")],
        [InlineKeyboardButton("🎯 هدف جديد", callback_data="learn_newgoal")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
    ]
    await query.edit_message_text(line, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def _learning_curriculum(services, user_id: int) -> dict:
    """First long-term goal + its first curriculum (or None)."""
    goals = services.learning_engine.goals(user_id)
    long_term = [g for g in goals if g.get("kind") == "long_term"]
    if not long_term:
        return {}
    goal = long_term[0]
    curricula = services.learning_engine.curricula(user_id, goal["goal_id"])
    curriculum = curricula[0] if curricula else None
    if curriculum is None:
        return {}
    return {"goal": goal, "curriculum": curriculum}


async def menu_learn_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start / continue the next lesson."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id

    pair = await _learning_curriculum(services, user_id)
    if not pair:
        keyboard = [[InlineKeyboardButton("🎯 هدف تعلم جديد", callback_data="learn_newgoal")],
                    [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
        await query.edit_message_text(
            "لا يوجد مسار تعلم بعد. ابدأ بإنشاء هدف.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    curriculum = pair["curriculum"]
    try:
        outcome = services.learning_engine.next_lesson(
            user_id, curriculum["curriculum_id"],
            grounded=bool(curriculum.get("source_id")),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Learning next_lesson failed for user {user_id}: {e}")
        await query.edit_message_text("❌ حدث خطأ أثناء تحضير الدرس. حاول مجدداً.")
        return
    if outcome is None:
        keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
        await query.edit_message_text("لا يوجد هدف تعليمي متاح بعد.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    context.user_data["learn_session_state"] = {
        "objective_id": outcome["objective"]["objective_id"],
        "session_id": (outcome["session"] or {}).get("session_id"),
    }
    lesson = outcome["lesson"]
    content = lesson.get("content") or {}
    text = (
        f"📖 **{content.get('title') or 'درس'}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**💡 الخلاصة:**\n{content.get('essence') or content.get('recap') or ''}\n\n"
        f"**📝 الشرح:**\n{content.get('explanation') or ''}\n\n"
        f"**▶️ الخطوة التالية:** {content.get('next_step') or ''}"
    )
    sources = lesson.get("sources") or []
    if sources:
        text += "\n\n**📚 من مرجعك:**"
        for s in sources[:3]:
            page = f" (ص. {s.get('page')})" if s.get("page") else ""
            text += f"\n• {s.get('title') or 'مرجع'}{page}"
            if s.get("section_title"):
                text += f" — {s['section_title']}"
    keyboard = [
        [InlineKeyboardButton("📝 ابدأ الاختبار السريع", callback_data="menu_learn_quiz")],
        [InlineKeyboardButton("🔁 المراجعة", callback_data="menu_learn_review")],
        [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def _start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      objective_id: int, session_id: int) -> None:
    services = get_services(context)
    user_id = update.effective_user.id
    try:
        questions = services.learning_engine.quick_check(
            user_id, session_id, objective_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Quick check failed for user {user_id}: {e}")
        await update.callback_query.answer("تعذر تجهيز الاختبار", show_alert=True)
        return
    context.user_data["learn_quiz"] = {
        "objective_id": objective_id,
        "session_id": session_id,
        "questions": questions,
        "answers": {},
    }
    await _render_quiz(update, context)


def _quiz_buttons(questions: list, answers: dict) -> list:
    keyboard = []
    for qi, question in enumerate(questions):
        if question.get("options"):
            row = []
            for oi, option in enumerate(question["options"]):
                label = ("✓ " if answers.get(qi) == option else "") + option
                row.append(InlineKeyboardButton(label, callback_data=f"learn_q_{qi}_{oi}"))
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✅ إنهاء الاختبار", callback_data="learn_quiz_done")])
    keyboard.append([InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")])
    return keyboard


async def _render_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = context.user_data.get("learn_quiz") or {}
    questions = state.get("questions") or []
    answers = state.get("answers") or {}
    lines = ["📝 **اختبار سريع**\n━━━━━━━━━━━━━━━━━━━━"]
    for qi, question in enumerate(questions):
        answered = " ✓" if answers.get(qi) is not None else ""
        lines.append(f"\n**س{qi + 1}. {question.get('prompt')}**{answered}")
    lines.append("\nاختر إجابتك ثم اضغط إنهاء.")
    text = "\n".join(lines)
    keyboard = _quiz_buttons(questions, answers)
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
    except Exception:  # noqa: BLE001 - message may be replaced
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                       parse_mode="Markdown")


async def menu_learn_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id

    state = _ready_lesson_state(context)
    if state is None:
        next_step = context.user_data.get("learn_session_state")
        objective_id = next_step.get("objective_id") if next_step else None
        session_id = next_step.get("session_id") if next_step else None
        if objective_id is None:
            outcome = await _resume_learning(services, user_id)
            if outcome is None:
                keyboard = [[InlineKeyboardButton("📖 الدرس التالي", callback_data="menu_learn_next")],
                            [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
                await query.edit_message_text(
                    "ابدأ الدرس أولاً حتى يكون لديك اختبار سريع.",
                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                return
            objective_id = outcome["objective"]["objective_id"]
            session_id = outcome.get("session_id")
            context.user_data["learn_session_state"] = {
                "objective_id": objective_id, "session_id": session_id,
            }
    else:
        objective_id, session_id = state
    await _start_quiz(update, context, objective_id, session_id)


def _ready_lesson_state(context) -> tuple:
    state = context.user_data.get("learn_session_state")
    if not state:
        return None
    return state.get("objective_id"), state.get("session_id")


async def _resume_learning(services, user_id: int):
    try:
        return services.learning_engine.continue_session(user_id)
    except Exception:  # noqa: BLE001
        return None


async def learn_quiz_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register one inline answer; grade when complete."""
    query = update.callback_query
    await query.answer()
    state = context.user_data.get("learn_quiz") or {}
    questions = state.get("questions") or []
    try:
        parts = query.data.split("_")
        qi, oi = int(parts[-2]), int(parts[-1])
    except (ValueError, AttributeError):
        await query.answer("اختيار غير صالح", show_alert=True)
        return
    if qi < 0 or qi >= len(questions):
        await query.answer("سؤال غير موجود", show_alert=True)
        return
    options = questions[qi].get("options") or []
    if oi < 0 or oi >= len(options):
        return
    answers = state.get("answers") or {}
    answers[qi] = options[oi]
    state["answers"] = answers
    context.user_data["learn_quiz"] = state
    remaining = [i for i in range(len(questions)) if i not in answers]
    if not remaining and questions:
        await _grade_quiz(update, context)
        return
    await _render_quiz(update, context)


async def learn_quiz_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = context.user_data.get("learn_quiz") or {}
    if (state.get("answers") or {}):
        await _grade_quiz(update, context)
        return
    await _render_quiz(update, context)


async def _grade_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    user_id = update.effective_user.id
    query = update.callback_query
    state = context.user_data.get("learn_quiz") or {}
    questions = state.get("questions") or []
    answers = state.get("answers") or {}
    objective_id = state.get("objective_id")
    session_id = state.get("session_id")
    context.user_data["learn_quiz"] = None
    if objective_id is None or not questions:
        await query.edit_message_text("لا يوجد اختبار نشط.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]))
        return
    try:
        graded = services.learning_engine.submit_quick_check(
            user_id, session_id, objective_id, answers, questions=questions,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Quick check grading failed for user {user_id}: {e}")
        await query.edit_message_text("❌ تعذر تقييم الاختبار. حاول مجدداً.")
        return
    result = graded["result"]
    text = (
        f"📊 **نتيجة الاختبار السريع**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"الدرجة: {result.get('score'):.0f}%  "
        f"({result.get('correct_count')}/{result.get('total')})\n\n"
        f"{result.get('feedback') or ''}\n\n"
        f"**الخطوة التالية:** {result.get('next_action') or ''}"
    )
    keyboard = [
        [InlineKeyboardButton("📖 الدرس التالي", callback_data="menu_learn_next")],
        [InlineKeyboardButton("🔁 المراجعة", callback_data="menu_learn_review")],
        [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def menu_learn_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Present the first due spaced-review item."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    try:
        due = services.learning_engine.due_reviews(user_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Due reviews failed for user {user_id}: {e}")
        await query.edit_message_text("❌ تعذر تحميل المراجعات.")
        return
    if not due:
        keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
        await query.edit_message_text(
            "🎉 لا توجد مراجعات مستحقة اليوم.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    first = due[0]
    first_id = int(first["review_id"])
    concept = first.get("concept") or first.get("objective_title") or "مفهوم"
    try:
        prompt = services.learning_engine.review_prompt(user_id, first_id)
    except Exception:  # noqa: BLE001
        prompt = f"استرجاع: راجع «{concept}»."
    text = f"🔁 **المراجعة المتخللة**\n━━━━━━━━━━━━━━━━━━━━\n\n{prompt}"
    keyboard = [
        [InlineKeyboardButton("✅ تذكرتها", callback_data=f"learn_r_yes_{first_id}"),
         InlineKeyboardButton("❌ نسيتها", callback_data=f"learn_r_no_{first_id}")],
        [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def learn_review_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grade a review answer and advance to the next due item."""
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    try:
        parts = query.data.split("_")
        verdict, review_id = parts[-2], int(parts[-1])
    except (ValueError, AttributeError):
        await query.answer("اختيار غير صالح", show_alert=True)
        return
    correct = verdict == "yes"
    reminder = "🎉 أحسنت! استمرار ممتاز." if correct else "💪 لا بأس، سيُعاد السؤال قريباً."
    try:
        services.learning_engine.record_review(user_id, review_id, correct)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Review grading failed for user {user_id}: {e}")
        await query.edit_message_text("❌ تعذر تسجيل المراجعة.")
        return
    due = services.learning_engine.due_reviews(user_id)
    if not due:
        keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
        await query.edit_message_text(
            f"{reminder}\n\n🎉 لا يوجد المزيد من المراجعات اليوم.",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return
    await menu_learn_review(update, context)


async def menu_learn_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    try:
        plan = services.learning_engine.daily_plan(user_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Learning plan failed for user {user_id}: {e}")
        await query.edit_message_text("❌ تعذر إعداد الخطة.")
        return
    lines = ["📋 **خطة تعلم اليوم**\n━━━━━━━━━━━━━━━━━━━━"]
    if not plan.get("items"):
        lines.append("لا توجد عناصر. أضف هدفاً تعليمياً أولاً.")
    for item in plan.get("items") or []:
        label = {"review": "🔁 مراجعة", "lesson": "📖 درس",
                 "habit": "🔄 عادة", "task": "✅ مهمة"}.get(item.get("kind"), "•")
        lines.append(f"{label} **{item.get('title')}** — {item.get('minutes')} دقيقة")
    if plan.get("adjusted"):
        lines.append("\n_خُفِّضت الخطة لتلائم وقتك المتاح هذه الفترة._")
    lines.append(f"\n⏱️ الإجمالي: {plan.get('total_minutes')} دقيقة")
    keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def menu_learn_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    services = get_services(context)
    user_id = update.effective_user.id
    try:
        week = services.learning_engine.weekly_review(user_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Weekly review failed for user {user_id}: {e}")
        await query.edit_message_text("❌ تعذر إعداد الملخص الأسبوعي.")
        return
    lines = [
        "📊 **ملخص الأسبوع**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الفترة: {week.get('period_start')} → {week.get('period_end')}",
        f"مخطط: {week.get('planned')} | منجز: {week.get('completed')}",
        f"جلسات تعلم: {week.get('learning_sessions')}",
    ]
    if week.get("improved"):
        lines.append(f"✅ تحسن: {', '.join(week['improved'][:3])}")
    if week.get("failed"):
        lines.append(f"⚠️ متراجع: {', '.join(week['failed'][:3])}")
    if week.get("weak_areas"):
        lines.append(f"🧱 نقاط ضعف: {', '.join(week['weak_areas'][:3])}")
    for i, change in enumerate((week.get("changes") or [])[:2], 1):
        lines.append(f"{i}. {change}")
    keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def learn_newgoal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the goal conversation."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]
    await query.edit_message_text(
        "🎯 **هدف تعليمي جديد**\n\nأرسل عنوان الهدف الطويل الأمد.\n"
        "مثال: تعلم لغة البرمجة بايثون",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return LEARN_GOAL_TITLE


async def learn_goal_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("أرسل اسم الهدف من فضلك.")
        return LEARN_GOAL_TITLE
    context.user_data["learn_pending_title"] = title
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_learn")]]
    await update.message.reply_text(
        "🌱 **أهداف التعليم**\n\nأرسل الأهداف التعليمية، هدف في كل سطر:\n"
        "مثال:\nالمتغيرات\nالحلقات\nالدوال",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return LEARN_GOAL_OBJECTIVES


async def learn_objectives_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    title = context.user_data.pop("learn_pending_title", "هدف تعلم")
    chain = services.learning_engine.create_goal_chain(
        user_id, vision=f"رؤيتي: {title}", long_term=title,
    )
    goal_id = chain.get("long_term_goal_id")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    objectives = []
    previous = None
    for line in lines:
        objective_id = services.learning_engine.add_objective(
            user_id, goal_id, title=line,
            prerequisites=[previous] if previous else None,
        )
        if objective_id:
            objectives.append(objective_id)
            previous = objective_id
    if goal_id and objectives:
        curriculum = services.learning_engine.build_curriculum(
            user_id, goal_id, title=title, mode="READ",
        )
        keyboard = [
            [InlineKeyboardButton("📖 ابدأ الدرس", callback_data="menu_learn_next")],
            [InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")],
        ]
        await update.message.reply_text(
            f"✅ أُنشئ الهدف «{title}» ومعه {len(objectives)} مادة تعليمية، "
            f"وتم تجهيز المنهج (وحدات {len((curriculum or {}).get('modules') or [])}).",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            "أُنشئ الهدف لكن لم تُضف مواد تعليمية. جرّب مرة أخرى.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 تعلم", callback_data="menu_learn")]]),
        )
    return ConversationHandler.END


# ==================== CONVERSATION HANDLERS FACTORY ====================

def get_learning_handlers() -> ConversationHandler:
    """Adaptive learning hub conversation (goal + objectives creation)."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(learn_newgoal, pattern="^learn_newgoal$")],
        states={
            LEARN_GOAL_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, learn_goal_title),
            ],
            LEARN_GOAL_OBJECTIVES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, learn_objectives_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_registration_handlers() -> ConversationHandler:
    """Get registration conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(start_registration, pattern="^start_registration$")],
        states={
            REG_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_age)],
            REG_EDUCATION: [CallbackQueryHandler(reg_education, pattern="^edu_")],
            REG_GOALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_goals)],
            REG_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_time)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_goal_handlers() -> ConversationHandler:
    """Get goal creation conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(goal_new, pattern="^goal_new$")],
        states={
            GOAL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_title)],
            GOAL_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_desc)],
            GOAL_CATEGORY: [CallbackQueryHandler(goal_category, pattern="^cat_")],
            GOAL_STAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_stages)],
            GOAL_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_deadline)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_plan_handlers() -> ConversationHandler:
    """Get plan creation conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(plan_new, pattern="^plan_new$")],
        states={
            PLAN_TASKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_tasks)],
            PLAN_PRIORITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_priorities)],
            PLAN_REVIEW_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_review_time)],
            PLAN_BREAK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_break_time)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_study_handlers() -> ConversationHandler:
    """Get study mode conversation handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(study_explain, pattern="^study_explain$"),
            CallbackQueryHandler(study_test, pattern="^study_test$"),
        ],
        states={
            STUDY_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, study_subject)],
            STUDY_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, study_topic)],
            STUDY_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, study_duration)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_english_handlers() -> ConversationHandler:
    """Get English mode conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(english_correct, pattern="^english_correct$")],
        states={
            ENGLISH_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, english_text)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_programming_handlers() -> ConversationHandler:
    """Get programming mode conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(prog_review, pattern="^prog_review$")],
        states={
            CODE_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_language)],
            CODE_REVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_review)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_accountability_handlers() -> ConversationHandler:
    """Get accountability review conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(review_daily, pattern="^review_daily$")],
        states={
            REVIEW_ACCOMPLISHED: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_accomplished)],
            REVIEW_LEARNED: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_learned)],
            REVIEW_OBSTACLES: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_obstacles)],
            REVIEW_TOMORROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_tomorrow)],
            REVIEW_MOOD: [CallbackQueryHandler(review_mood, pattern="^mood_")],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_reminder_handlers() -> ConversationHandler:
    """Get reminder creation conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(reminder_new, pattern="^reminder_new$")],
        states={
            REMINDER_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_title)],
            REMINDER_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_message)],
            REMINDER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_time)],
            REMINDER_CONFIRM: [CallbackQueryHandler(reminder_confirm, pattern="^reminder_confirm$")],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_habit_handlers() -> ConversationHandler:
    """Get habit creation conversation handler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(habit_new, pattern="^habit_new$")],
        states={
            HABIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_name)],
            HABIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_desc)],
            HABIT_FREQUENCY: [CallbackQueryHandler(habit_frequency, pattern="^freq_")],
            HABIT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_time)],
        },
        fallbacks=[CommandHandler("cancel", fallback_cancel)],
        per_message=False,
    )


def get_menu_handlers() -> list:
    """Get all menu callback handlers."""
    return [
        CallbackQueryHandler(menu_main, pattern="^menu_main$"),
        CallbackQueryHandler(settings_profile, pattern="^settings_profile$"),
        CallbackQueryHandler(settings_reminders, pattern="^settings_reminders$"),
        CallbackQueryHandler(menu_goals, pattern="^menu_goals$"),
        CallbackQueryHandler(goal_list, pattern="^goal_list$"),
        CallbackQueryHandler(menu_plan, pattern="^menu_plan$"),
        CallbackQueryHandler(plan_today, pattern="^plan_today$"),
        CallbackQueryHandler(menu_discipline, pattern="^menu_discipline$"),
        CallbackQueryHandler(discipline_check, pattern="^discipline_check$"),
        CallbackQueryHandler(menu_study, pattern="^menu_study$"),
        CallbackQueryHandler(menu_english, pattern="^menu_english$"),
        CallbackQueryHandler(english_lesson, pattern="^english_lesson$"),
        CallbackQueryHandler(menu_programming, pattern="^menu_programming$"),
        CallbackQueryHandler(menu_habits, pattern="^menu_habits$"),
        CallbackQueryHandler(habit_list, pattern="^habit_list$"),
        CallbackQueryHandler(menu_progress, pattern="^menu_progress$"),
        CallbackQueryHandler(menu_coach, pattern="^menu_coach$"),
        CallbackQueryHandler(coach_analysis, pattern="^coach_analysis$"),
        CallbackQueryHandler(menu_dashboard, pattern="^menu_dashboard$"),
        CallbackQueryHandler(menu_review, pattern="^menu_review$"),
        CallbackQueryHandler(review_history, pattern="^review_history$"),
        CallbackQueryHandler(menu_settings, pattern="^menu_settings$"),
        CallbackQueryHandler(settings_ai, pattern="^settings_ai$"),
        CallbackQueryHandler(settings_ai_reconnect, pattern="^settings_ai_reconnect$"),
        CallbackQueryHandler(menu_knowledge, pattern="^menu_knowledge$"),
        CallbackQueryHandler(knowledge_books, pattern="^knowledge_books$"),
        CallbackQueryHandler(knowledge_upload, pattern="^knowledge_upload$"),
        CallbackQueryHandler(knowledge_search, pattern="^knowledge_search$"),
        CallbackQueryHandler(knowledge_collections, pattern="^knowledge_collections$"),
        CallbackQueryHandler(knowledge_processing, pattern="^knowledge_processing$"),
        CallbackQueryHandler(knowledge_settings, pattern="^knowledge_settings$"),
        CallbackQueryHandler(menu_memory, pattern="^menu_memory$"),
        CallbackQueryHandler(memory_view, pattern="^memory_view$"),
        CallbackQueryHandler(memory_search, pattern="^memory_search$"),
        CallbackQueryHandler(memory_edit, pattern="^memory_edit$"),
        CallbackQueryHandler(memory_forget, pattern="^memory_forget$"),
        CallbackQueryHandler(memory_clear, pattern="^memory_clear$"),
        CallbackQueryHandler(memory_clear_confirm, pattern="^memory_clear_confirm$"),
        CallbackQueryHandler(memory_settings, pattern="^memory_settings$"),
        CallbackQueryHandler(memory_toggle, pattern="^memory_toggle$"),
        CallbackQueryHandler(memory_pending, pattern="^memory_pending$"),
        CallbackQueryHandler(memory_consent, pattern="^memory_(accept|reject)_\\d+$"),
        CallbackQueryHandler(memory_export, pattern="^memory_export$"),
        # Adaptive learning hub (Phase E).
        CallbackQueryHandler(menu_learn, pattern="^menu_learn$"),
        CallbackQueryHandler(menu_learn_next, pattern="^menu_learn_next$"),
        CallbackQueryHandler(menu_learn_quiz, pattern="^menu_learn_quiz$"),
        CallbackQueryHandler(learn_quiz_btn, pattern="^learn_q_\\d+_\\d+$"),
        CallbackQueryHandler(learn_quiz_done, pattern="^learn_quiz_done$"),
        CallbackQueryHandler(menu_learn_review, pattern="^menu_learn_review$"),
        CallbackQueryHandler(learn_review_grade, pattern="^learn_r_(yes|no)_\\d+$"),
        CallbackQueryHandler(menu_learn_plan, pattern="^menu_learn_plan$"),
        CallbackQueryHandler(menu_learn_week, pattern="^menu_learn_week$"),
        CallbackQueryHandler(learn_newgoal, pattern="^learn_newgoal$"),
    ]


async def review_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show review history."""
    services = get_services(context)
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    reviews = services.reviews.history(user_id, days=7)

    if not reviews:
        keyboard = [[InlineKeyboardButton("🌙 محاسبة جديدة", callback_data="review_daily")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "لا توجد مراجعات سابقة.",
            reply_markup=reply_markup,
        )
        return

    history_text = "📋 **مراجعاتك الأخيرة:**\n\n"
    for review in reviews[:5]:
        mood_emoji = {5: "😊", 4: "🙂", 3: "😐", 2: "🙁", 1: "😢"}.get(
            review.get("mood", 3), "😐"
        )
        history_text += f"📅 {review['date']} {mood_emoji}\n"
        history_text += f"   ✅ {review.get('accomplished', '')[:50]}...\n\n"

    keyboard = [
        [InlineKeyboardButton("🌙 محاسبة جديدة", callback_data="review_daily")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_review")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        history_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )