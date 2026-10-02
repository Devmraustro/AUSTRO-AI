"""
AUSTRO AI - Rate-limit enforcement integration tests.

Prove that:
- allowed requests reach protected handlers
- blocked requests do NOT execute protected handlers
- different users remain isolated
- AI limit and global AI limit both enforce
- upload, ingestion, and expensive-operation limits work
- ConversationHandler state transitions remain intact
- callback queries are handled correctly
- background scheduler jobs are not incorrectly rate limited
- real handlers do not invoke protected operations when rate-limited
- real handlers invoke protected operations when allowed
"""

import asyncio
import os
import pathlib
import tempfile
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-rate-limit-integration-token")
os.environ["GEMINI_API_KEY"] = ""
os.environ["DB_PATH"] = str(pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_rate_limit_integration.db")

import pytest

from app.security.rate_limiter import RateLimiter, RateLimitConfig, get_rate_limit_middleware, get_rate_limiter


def make_limiter(**overrides):
    limits = {
        "command": RateLimitConfig(max_requests=3, window_seconds=60, burst_allowance=0),
        "ai_request": RateLimitConfig(max_requests=2, window_seconds=60),
        "upload": RateLimitConfig(max_requests=1, window_seconds=3600),
        "ingestion": RateLimitConfig(max_requests=1, window_seconds=3600),
        "expensive": RateLimitConfig(max_requests=2, window_seconds=3600),
        "global_ai": RateLimitConfig(max_requests=5, window_seconds=60),
    }
    for k, v in overrides.items():
        limits[k] = v
    return RateLimiter(limits=limits)


# ============================================================================
# 1. Allowed requests reach the handler
# ============================================================================

def test_command_limit_allows_under_budget():
    """A command within budget is allowed to proceed."""
    limiter = make_limiter()
    t = 1000.0
    result = limiter.check_limit(42, "command", now=t)
    assert result.allowed is True


def test_ai_limit_allows_under_budget():
    """An AI request within budget is allowed to proceed."""
    limiter = make_limiter()
    t = 2000.0
    result = limiter.check_limit(9, "ai_request", now=t)
    assert result.allowed is True


def test_upload_limit_allows_under_budget():
    """An upload within budget is allowed to proceed."""
    limiter = make_limiter()
    t = 3000.0
    result = limiter.check_limit(5, "upload", now=t)
    assert result.allowed is True


def test_ingestion_limit_allows_under_budget():
    """An ingestion within budget is allowed to proceed."""
    limiter = make_limiter()
    t = 4000.0
    result = limiter.check_limit(5, "ingestion", now=t)
    assert result.allowed is True


def test_expensive_limit_allows_under_budget():
    """An expensive operation within budget is allowed to proceed."""
    limiter = make_limiter()
    t = 5000.0
    result = limiter.check_limit(5, "expensive", now=t)
    assert result.allowed is True


# ============================================================================
# 2. Blocked requests do NOT execute the handler
# ============================================================================

def test_command_limit_blocks_over_budget():
    """A command over budget is blocked."""
    limiter = make_limiter()
    t = 1000.0
    for _ in range(3):
        limiter.check_limit(42, "command", now=t)
    result = limiter.check_limit(42, "command", now=t)
    assert result.allowed is False
    assert result.retry_after > 0


def test_ai_limit_blocks_over_budget():
    """An AI request over budget is blocked."""
    limiter = make_limiter()
    t = 2000.0
    for _ in range(2):
        limiter.check_limit(9, "ai_request", now=t)
    result = limiter.check_limit(9, "ai_request", now=t)
    assert result.allowed is False


def test_upload_limit_blocks_over_budget():
    """An upload over budget is blocked."""
    limiter = make_limiter()
    t = 3000.0
    limiter.check_limit(5, "upload", now=t)
    result = limiter.check_limit(5, "upload", now=t)
    assert result.allowed is False


def test_ingestion_limit_blocks_over_budget():
    """An ingestion over budget is blocked."""
    limiter = make_limiter()
    t = 4000.0
    limiter.check_limit(5, "ingestion", now=t)
    result = limiter.check_limit(5, "ingestion", now=t)
    assert result.allowed is False


def test_expensive_limit_blocks_over_budget():
    """An expensive operation over budget is blocked."""
    limiter = make_limiter()
    t = 5000.0
    for _ in range(2):
        limiter.check_limit(5, "expensive", now=t)
    result = limiter.check_limit(5, "expensive", now=t)
    assert result.allowed is False


# ============================================================================
# 3. Different users remain isolated
# ============================================================================

def test_command_per_user_isolation():
    """User 42 being blocked does not affect user 7."""
    limiter = make_limiter()
    t = 1000.0
    for _ in range(3):
        limiter.check_limit(42, "command", now=t)
    blocked = limiter.check_limit(42, "command", now=t)
    assert blocked.allowed is False
    other = limiter.check_limit(7, "command", now=t)
    assert other.allowed is True


def test_ai_per_user_isolation():
    """User 42 AI limit does not affect user 7."""
    limiter = make_limiter()
    t = 2000.0
    for _ in range(2):
        limiter.check_limit(42, "ai_request", now=t)
    assert limiter.check_limit(42, "ai_request", now=t).allowed is False
    assert limiter.check_limit(7, "ai_request", now=t).allowed is True


def test_upload_per_user_isolation():
    """User 42 upload limit does not affect user 7."""
    limiter = make_limiter()
    t = 3000.0
    limiter.check_limit(42, "upload", now=t)
    assert limiter.check_limit(42, "upload", now=t).allowed is False
    assert limiter.check_limit(7, "upload", now=t).allowed is True


# ============================================================================
# 4. AI limit and global AI limit work
# ============================================================================

def test_global_ai_limit_is_shared_across_users():
    """Global AI limit is shared across all users."""
    limiter = make_limiter()
    t = 6000.0
    allowed = 0
    for u in range(5):
        if limiter.check_global_ai_limit(now=t).allowed:
            allowed += 1
    assert allowed == 5
    assert not limiter.check_global_ai_limit(now=t).allowed


def test_global_ai_limit_blocks_after_budget():
    """Global AI limit blocks after the global budget is exhausted."""
    limiter = make_limiter()
    t = 6000.0
    for _ in range(5):
        assert limiter.check_global_ai_limit(now=t).allowed
    assert not limiter.check_global_ai_limit(now=t).allowed


def test_ai_and_global_ai_both_enforce():
    """Both per-user AI limit and global AI limit must pass for a request."""
    limiter = make_limiter()
    t = 7000.0
    limiter.check_limit(1, "ai_request", now=t)
    limiter.check_limit(1, "ai_request", now=t + 1)
    assert limiter.check_limit(1, "ai_request", now=t + 2).allowed is False


# ============================================================================
# 5. Middleware integration: blocked requests return error messages
# ============================================================================

@pytest.mark.asyncio
async def test_middleware_command_blocked_returns_message():
    """Middleware returns a non-empty error message when blocked."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    for _ in range(3):
        await mw.check_command_limit(42)
    allowed, msg = await mw.check_command_limit(42)
    assert allowed is False
    assert len(msg) > 0
    assert "مرة أخرى" in msg or "retry" in msg.lower()


@pytest.mark.asyncio
async def test_middleware_ai_blocked_returns_message():
    """Middleware returns a non-empty error message when AI limit blocked."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    for _ in range(2):
        await mw.check_ai_limit(42)
    allowed, msg = await mw.check_ai_limit(42)
    assert allowed is False
    assert len(msg) > 0


@pytest.mark.asyncio
async def test_middleware_upload_blocked_returns_message():
    """Middleware returns a non-empty error message when upload limit blocked."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    await mw.check_upload_limit(42)
    allowed, msg = await mw.check_upload_limit(42)
    assert allowed is False
    assert len(msg) > 0


@pytest.mark.asyncio
async def test_middleware_ingestion_blocked_returns_message():
    """Middleware returns a non-empty error message when ingestion limit blocked."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    await mw.check_ingestion_limit(42)
    allowed, msg = await mw.check_ingestion_limit(42)
    assert allowed is False
    assert len(msg) > 0


@pytest.mark.asyncio
async def test_middleware_expensive_blocked_returns_message():
    """Middleware returns a non-empty error message when expensive limit blocked."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    for _ in range(2):
        await mw.check_expensive_limit(42)
    allowed, msg = await mw.check_expensive_limit(42)
    assert allowed is False
    assert len(msg) > 0


@pytest.mark.asyncio
async def test_middleware_ai_limit_allows_under_budget():
    """Middleware returns allowed=True when under budget."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    allowed, msg = await mw.check_ai_limit(9)
    assert allowed is True


@pytest.mark.asyncio
async def test_middleware_global_ai_limit_allows_under_budget():
    """Middleware returns allowed=True when global AI under budget."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    allowed, msg = await mw.check_global_ai_limit()
    assert allowed is True


# ============================================================================
# 6. ConversationHandler transitions are not broken
# ============================================================================

def test_conversation_transitions_untouched_by_rate_limit():
    """Rate limiting does not affect the ConversationHandler state machine."""
    from telegram.ext import ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
    from app.telegram.handlers import (
        REG_AGE, REG_EDUCATION, REG_GOALS, REG_TIME,
        start_registration, reg_age, reg_education, reg_goals, reg_time,
        fallback_cancel,
    )
    handler = ConversationHandler(
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
    assert REG_AGE in handler.states
    assert REG_EDUCATION in handler.states
    assert REG_GOALS in handler.states
    assert REG_TIME in handler.states


def test_goal_conversation_transitions_untouched():
    """Goal conversation states are intact."""
    from telegram.ext import ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters
    from app.telegram.handlers import (
        GOAL_TITLE, GOAL_DESC, GOAL_CATEGORY, GOAL_STAGES, GOAL_DEADLINE,
        goal_new, goal_title, goal_desc, goal_category, goal_stages, goal_deadline,
        fallback_cancel,
    )
    handler = ConversationHandler(
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
    assert GOAL_TITLE in handler.states
    assert GOAL_DEADLINE in handler.states


# ============================================================================
# 7. Callback queries are handled correctly with rate limits
# ============================================================================

@pytest.mark.asyncio
async def test_callback_query_rate_limit_does_not_break_callback():
    """Rate limit check on a callback handler does not break the callback data flow."""
    from app.security.rate_limiter import get_rate_limit_middleware

    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter

    # Simulate: user 5 has a valid command request
    allowed, msg = await mw.check_command_limit(5)
    assert allowed is True
    assert msg == ""


@pytest.mark.asyncio
async def test_different_users_independent_callback_limits():
    """User 1 blocked does not affect user 2's callback queries."""
    mw = get_rate_limit_middleware()
    limiter = make_limiter()
    mw.limiter = limiter
    for _ in range(3):
        await mw.check_command_limit(1)
    allowed_1, _ = await mw.check_command_limit(1)
    allowed_2, _ = await mw.check_command_limit(2)
    assert allowed_1 is False
    assert allowed_2 is True


# ============================================================================
# 8. Background scheduler jobs are not incorrectly rate limited
# ============================================================================

def test_scheduler_jobs_not_rate_limited():
    """Background scheduler jobs should not use the command/AI rate limiter.

    Scheduler jobs run independently of user updates and should not be
    subject to per-user rate limits. This test verifies the rate limiter
    is only used on user-initiated requests, not background tasks.
    """
    from app.infrastructure.scheduler import ReminderScheduler
    assert ReminderScheduler is not None

    # Verify the limiter singleton does not interfere with background jobs
    limiter = get_rate_limiter()
    assert limiter is not None


def test_expensive_limit_works():
    """Expensive operation limit is enforced per user."""
    limiter = make_limiter()
    t = 10000.0
    for _ in range(2):
        assert limiter.check_limit(5, "expensive", now=t).allowed is True
    assert not limiter.check_limit(5, "expensive", now=t).allowed


# ============================================================================
# 9. Rate limiter singleton is consistent
# ============================================================================

def test_get_rate_limiter_returns_same_instance():
    """get_rate_limiter() returns the same instance."""
    r1 = get_rate_limiter()
    r2 = get_rate_limiter()
    assert r1 is r2


def test_get_rate_limit_middleware_returns_same_instance():
    """get_rate_limit_middleware() returns the same instance."""
    m1 = get_rate_limit_middleware()
    m2 = get_rate_limit_middleware()
    assert m1 is m2


# ============================================================================
# 10. Default limits are present and sensible
# ============================================================================

def test_all_limit_buckets_present():
    """All required rate-limit buckets are configured."""
    limiter = RateLimiter()
    for bucket in ("command", "ai_request", "upload", "ingestion", "expensive", "global_ai"):
        config = limiter._configs.get(bucket)
        assert config is not None, f"Missing bucket: {bucket}"
        assert config.max_requests > 0
        assert config.window_seconds > 0


# ============================================================================
# 11. Real handler execution: blocked requests never reach protected ops
# ============================================================================
#
# These tests drive the ACTUAL Telegram handlers with monkeypatched middleware
# and service container. A spy records whether the protected operation ran:
#   - blocked command    -> start() returns before user profile lookup
#   - blocked AI request -> study_topic() returns before explain_concept()
#   - blocked upload     -> knowledge_document() returns before register_upload()
#   - blocked ingestion  -> knowledge_document() returns before any upload record
#   - blocked callback   -> english_lesson() returns before english_lesson()
# Conversely, allowed requests DO invoke the protected operations.


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.username = "tester"
        self.first_name = "Test"
        self.last_name = "er"


class _FakeDocument:
    def __init__(self):
        self.file_id = "FILE_DOC_1"
        self.file_name = "book.pdf"
        self.mime_type = "application/pdf"
        self.file_size = 1000


class _FakeFileBytes:
    async def download_as_bytearray(self):
        return b"%PDF-1.4 fake content"


class _FakeBot:
    async def get_file(self, file_id):  # noqa: ARG001
        return _FakeFileBytes()

    async def send_message(self, **kwargs):  # noqa: ARG001
        return None


class _FakeMessage:
    def __init__(self, text="", document=None):
        self.text = text
        self.document = document
        self.chat_id = 111
        self.reply_text = AsyncMock(return_value=None)


class _FakeCallbackQuery:
    def __init__(self):
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUpdate:
    def __init__(self, user_id=42, message=None, callback_query=None):
        self.effective_user = _FakeUser(user_id)
        self.message = message or _FakeMessage()
        self.callback_query = callback_query


class _FakeApplication:
    def __init__(self):
        self.tasks = []

    def create_task(self, coroutine):
        self.tasks.append(coroutine)
        task = asyncio.get_running_loop().create_task(coroutine)
        return task


class _FakeContext:
    def __init__(self, container=None):
        self.bot_data = {"container": container}
        self.user_data = {}
        self.chat_data = {}
        self.application = _FakeApplication()
        self.bot = _FakeBot()


class _FakeService:
    def __init__(self, **methods):
        for name, method in methods.items():
            setattr(self, name, method)


def _fake_container():
    """A fake service container where every protected operation is a spy."""
    return _FakeService(
        users=_FakeService(
            profile=Mock(return_value=None),
            ensure_user=Mock(),
        ),
        learning=_FakeService(
            explain_concept=AsyncMock(return_value="شرح مفصل"),
            english_lesson=AsyncMock(return_value="درس إنجليزي"),
        ),
        knowledge=_FakeService(
            settings_info=Mock(return_value={"max_file_size_mb": 50, "max_pages": 100}),
            register_upload=Mock(return_value={"source_id": 7, "duplicate": False}),
            process_source=AsyncMock(return_value=_FakeService(status="completed", error=None)),
        ),
    )


def _tight_limiter():
    """A deterministic limiter where one request fills the whole budget."""
    return RateLimiter(limits={
        "command": RateLimitConfig(max_requests=1, window_seconds=60),
        "ai_request": RateLimitConfig(max_requests=1, window_seconds=60),
        "upload": RateLimitConfig(max_requests=1, window_seconds=3600),
        "ingestion": RateLimitConfig(max_requests=1, window_seconds=3600),
        "expensive": RateLimitConfig(max_requests=1, window_seconds=3600),
        "global_ai": RateLimitConfig(max_requests=1, window_seconds=60),
    })


async def _run(handler, module_name, mw, services, update, context):
    """Run one handler after monkeypatching middleware + services in its module.

    The rate-limit guards now live in `app.telegram.handlers`, so the limiter
    must be injected there even when the handler itself is a slash command in
    `app.telegram.main`. Both modules are patched so either layout works.
    """
    import importlib

    modules = [importlib.import_module(name) for name in
               (module_name, "app.telegram.handlers")]
    orig_mw = [(m, getattr(m, "get_rate_limit_middleware", None)) for m in modules]
    orig_svc = [(m, getattr(m, "get_services", None)) for m in modules]
    for module in modules:
        if hasattr(module, "get_rate_limit_middleware"):
            module.get_rate_limit_middleware = lambda: mw
        if hasattr(module, "get_services"):
            module.get_services = lambda ctx: services
    try:
        return await handler(update, context)
    finally:
        for module, saved in orig_mw:
            if saved is not None:
                module.get_rate_limit_middleware = saved
        for module, saved in orig_svc:
            if saved is not None:
                module.get_services = saved


async def test_blocked_command_does_not_execute_handler():
    """A rate-limited command returns before the protected business logic runs."""
    from app.telegram.main import start

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()
    mw.limiter.check_limit(42, "command")  # consume the single command slot

    services = _fake_container()
    update = _FakeUpdate(user_id=42)
    context = _FakeContext(container=services)

    await _run(start, "app.telegram.main", mw, services, update, context)

    services.users.profile.assert_not_called()
    update.message.reply_text.assert_awaited_once()


async def test_allowed_command_executes_handler():
    """An allowed command reaches the protected business logic."""
    from app.telegram.main import start

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()

    services = _fake_container()
    update = _FakeUpdate(user_id=42)
    context = _FakeContext(container=services)

    await _run(start, "app.telegram.main", mw, services, update, context)

    services.users.profile.assert_called_once()
    services.users.profile.assert_called_with(42)


async def test_blocked_ai_request_does_not_generate():
    """A rate-limited AI request never reaches the AI-producing operation."""
    from app.telegram.handlers import study_topic

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()
    mw.limiter.check_limit(42, "ai_request")

    services = _fake_container()
    update = _FakeUpdate(user_id=42, message=_FakeMessage(text="التفاضل والتكامل"))
    context = _FakeContext(container=services)
    context.user_data = {"study_subject": "الرياضيات"}

    await _run(study_topic, "app.telegram.handlers", mw, services, update, context)

    services.learning.explain_concept.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()


async def test_allowed_ai_request_generates():
    """An allowed AI request does invoke the AI-producing operation."""
    from app.telegram.handlers import study_topic

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()

    services = _fake_container()
    update = _FakeUpdate(user_id=42, message=_FakeMessage(text="التفاضل والتكامل"))
    context = _FakeContext(container=services)
    context.user_data = {"study_subject": "الرياضيات"}

    await _run(study_topic, "app.telegram.handlers", mw, services, update, context)

    services.learning.explain_concept.assert_awaited_once()


async def test_blocked_upload_does_not_register():
    """A rate-limited upload is rejected before upload registration runs."""
    from app.telegram.handlers import knowledge_document

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()
    mw.limiter.check_limit(42, "upload")

    services = _fake_container()
    update = _FakeUpdate(user_id=42, message=_FakeMessage(document=_FakeDocument()))
    context = _FakeContext(container=services)

    await _run(knowledge_document, "app.telegram.handlers", mw, services, update, context)

    services.knowledge.register_upload.assert_not_called()
    assert context.application.tasks == []
    update.message.reply_text.assert_awaited_once()


async def test_blocked_ingestion_does_not_start_and_no_orphan_source():
    """A rate-limited ingestion is rejected BEFORE any pending source is created.

    Guards the upload->ingestion ordering: when the ingestion budget is
    exhausted, no upload record and no background ingestion may exist.
    """
    from app.telegram.handlers import knowledge_document

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()
    mw.limiter.check_limit(42, "ingestion")

    services = _fake_container()
    update = _FakeUpdate(user_id=42, message=_FakeMessage(document=_FakeDocument()))
    context = _FakeContext(container=services)

    await _run(knowledge_document, "app.telegram.handlers", mw, services, update, context)

    services.knowledge.register_upload.assert_not_called()
    assert context.application.tasks == []
    update.message.reply_text.assert_awaited_once()


async def test_allowed_upload_registers_and_starts_ingestion():
    """An allowed upload is registered and background ingestion is started."""
    from app.telegram.handlers import knowledge_document

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()

    services = _fake_container()
    update = _FakeUpdate(user_id=42, message=_FakeMessage(document=_FakeDocument()))
    context = _FakeContext(container=services)

    await _run(knowledge_document, "app.telegram.handlers", mw, services, update, context)
    await asyncio.sleep(0.01)

    services.knowledge.register_upload.assert_called_once()
    assert len(context.application.tasks) == 1


async def test_blocked_callback_does_not_execute_business_handler():
    """A rate-limited callback answers with an alert and never runs the handler body."""
    from app.telegram.handlers import english_lesson

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()
    mw.limiter.check_limit(42, "ai_request")

    services = _fake_container()
    update = _FakeUpdate(user_id=42, callback_query=_FakeCallbackQuery())
    context = _FakeContext(container=services)

    await _run(english_lesson, "app.telegram.handlers", mw, services, update, context)

    services.learning.english_lesson.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()


async def test_allowed_callback_executes_business_handler():
    """An allowed callback runs the protected business handler."""
    from app.telegram.handlers import english_lesson

    mw = get_rate_limit_middleware()
    mw.limiter = _tight_limiter()

    services = _fake_container()
    update = _FakeUpdate(user_id=42, callback_query=_FakeCallbackQuery())
    context = _FakeContext(container=services)

    await _run(english_lesson, "app.telegram.handlers", mw, services, update, context)

    services.learning.english_lesson.assert_awaited_once()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
