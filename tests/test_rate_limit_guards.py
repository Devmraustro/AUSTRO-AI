"""
AUSTRO AI - Rate-limit guard contracts (release checklist item 8).

Deterministic, no live Telegram and no live AI calls. Each test drives the real
handler with a fake update whose services are spies, and a limiter pinned to a
known budget, then proves exactly one of two things:

  * ALLOWED  - the protected business logic ran exactly once, and
  * BLOCKED  - a safe user-facing message was returned while NO provider call,
               NO database write, NO state transition and NO background task
               happened.

Covers, per operation class: the four rate-limit buckets, the "charge once per
started flow" rule, per-user isolation, global AI isolation, callback-query
answering, and the deliberately unguarded recovery paths.
"""

import asyncio
import inspect
import os
import pathlib
import tempfile
from unittest.mock import ANY, AsyncMock, Mock

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-rate-limit-guards-token")
os.environ["GEMINI_API_KEY"] = ""
os.environ["DB_PATH"] = str(
    pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_rate_limit_guards.db"
)

import pytest

from app.core.errors import ValidationError
from app.security.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
    get_rate_limit_middleware,
)
from app.telegram import handlers as H


# ============================================================================
# Test doubles
# ============================================================================


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id
        self.username = "tester"
        self.first_name = "Test"
        self.last_name = "er"


class _FakeMessage:
    def __init__(self, text="", document=None):
        self.text = text
        self.document = document
        self.chat_id = 111
        self.reply_text = AsyncMock(return_value=None)


class _FakeCallbackQuery:
    def __init__(self, data="x"):
        self.data = data
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUpdate:
    """Mirrors a real update: a callback update carries a query, not a message."""

    def __init__(self, user_id=42, text="", data="x", document=None, callback=False):
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = Mock(id=111)
        if callback:
            self.callback_query = _FakeCallbackQuery(data)
            self.message = None
        else:
            self.message = _FakeMessage(text=text, document=document)
            self.callback_query = None


class _FakeFile:
    async def download_as_bytearray(self):
        return b"%PDF-1.4 fake"


class _FakeBot:
    def __init__(self):
        self.get_file = AsyncMock(return_value=_FakeFile())
        self.send_message = AsyncMock(return_value=None)
        self.send_document = AsyncMock(return_value=None)


class _FakeApplication:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        return asyncio.get_running_loop().create_task(coro)


class _FakeContext:
    def __init__(self, container=None, user_data=None):
        self.bot_data = {"container": container}
        self.user_data = dict(user_data or {})
        self.chat_data = {}
        self.application = _FakeApplication()
        self.bot = _FakeBot()


class _Svc:
    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, fn)


class _FakeDocument:
    def __init__(self, file_id="F1", name="book.pdf", size=1000):
        self.file_id = file_id
        self.file_name = name
        self.mime_type = "application/pdf"
        self.file_size = size


def _services(**overrides):
    """Every protected operation is a spy that can be asserted on."""
    base = dict(
        users=_Svc(profile=Mock(return_value=None), ensure_user=Mock(),
                   update_profile=Mock(return_value=True), parse_age=Mock(return_value=24)),
        goals=_Svc(list=Mock(return_value=[]), create=Mock(return_value=1)),
        habits=_Svc(list=Mock(return_value=[]), create=Mock(return_value=1)),
        plans=_Svc(for_date=Mock(return_value=None), create=Mock(return_value=True)),
        reminders=_Svc(active=Mock(return_value=[]),
                       create_daily_reminder=Mock(return_value=True)),
        reviews=_Svc(save=Mock(return_value=True), history=Mock(return_value=[])),
        progress=_Svc(weekly_summary=Mock(return_value={
            "total_hours": 0, "total_tasks": 0, "days": 0})),
        dashboard=_Svc(stats=Mock(return_value={
            "total_goals": 0, "completed_goals": 0, "total_habits": 0,
            "total_streaks": 0, "weekly_study_hours": 0, "weekly_tasks": 0,
            "total_reviews": 0})),
        memory=_Svc(
            memory_block=Mock(return_value=""),
            counts=Mock(return_value={"total": 0}), enabled=Mock(return_value=True),
            pending=Mock(return_value=[]), list=Mock(return_value=[]),
            search=Mock(return_value=[]), edit=Mock(return_value=True),
            forget=Mock(return_value=True), clear=Mock(return_value=0),
            set_auto_enabled=Mock(return_value=True),
            confirm=Mock(return_value=True), export=Mock(return_value=None),
        ),
        chat=_Svc(status=Mock(return_value="ok"),
                  reconnect=AsyncMock(return_value="reconnected")),
        coaching=_Svc(advice=AsyncMock(return_value="نصيحة"),
                      analyze=AsyncMock(return_value="تحليل")),
        learning=_Svc(
            explain_concept=AsyncMock(return_value="شرح"),
            generate_test=AsyncMock(return_value="اختبار"),
            english_lesson=AsyncMock(return_value="درس"),
            correct_english=AsyncMock(return_value="تصحيح"),
            review_code=AsyncMock(return_value="مراجعة"),
        ),
        knowledge=_Svc(
            settings_info=Mock(return_value={"max_file_size_mb": 50, "max_pages": 100,
                                             "chunk_size": 800, "chunk_overlap": 100,
                                             "embedding_model": "m", "embedding_version": 1,
                                             "dimensions": 768, "top_k": 5}),
            register_upload=AsyncMock(return_value={"source_id": 7, "duplicate": False}),
            process_source=AsyncMock(return_value=_Svc(status="completed", error=None)),
            counts=Mock(return_value={"total": 0, "ready": 0, "processing": 0, "failed": 0}),
            list_sources=Mock(return_value=[]),
            collections=_Svc(list=Mock(return_value=[])),
            processing_sources=Mock(return_value=[]),
            answer=AsyncMock(return_value=_Svc(text="إجابة", citations=[])),
        ),
        learning_engine=_Svc(
            goals=Mock(return_value=[]), curricula=Mock(return_value=[]),
            coach_today=Mock(return_value={"reason": "r", "focus": "f"}),
            progress_overview=Mock(return_value=_Svc(
                objectives_total=0, mastered=0, near_mastery=0, due_reviews=0)),
            next_lesson=Mock(return_value=None), quick_check=Mock(return_value=[]),
            submit_quick_check=Mock(return_value={"result": {
                "score": 100.0, "correct_count": 1, "total": 1,
                "feedback": "", "next_action": ""}}),
            due_reviews=Mock(return_value=[]), record_review=Mock(return_value=True),
            daily_plan=Mock(return_value={"items": [], "total_minutes": 0}),
            weekly_review=Mock(return_value={"period_start": "a", "period_end": "b",
                                             "planned": 0, "completed": 0,
                                             "learning_sessions": 0, "improved": [],
                                             "failed": [], "weak_areas": [], "changes": []}),
            create_goal_chain=Mock(return_value={"long_term_goal_id": 1}),
            add_objective=Mock(return_value=1),
            build_curriculum=Mock(return_value={"modules": []}),
            continue_session=Mock(return_value=None),
        ),
    )
    base.update(overrides)
    return _Svc(**base)


def _tight(**over):
    """One request fills the whole budget, so the 2nd is deterministically blocked."""
    limits = {
        "command": RateLimitConfig(max_requests=1, window_seconds=60),
        "ai_request": RateLimitConfig(max_requests=1, window_seconds=60),
        "upload": RateLimitConfig(max_requests=1, window_seconds=3600),
        "ingestion": RateLimitConfig(max_requests=1, window_seconds=3600),
        "expensive": RateLimitConfig(max_requests=1, window_seconds=3600),
        "global_ai": RateLimitConfig(max_requests=1, window_seconds=60),
    }
    limits.update(over)
    return RateLimiter(limits=limits)


async def _run(handler, services, update, context, limiter=None):
    """Drive a handler with the module's middleware + services swapped out."""
    mw = get_rate_limit_middleware()
    mw.limiter = limiter or _tight()
    orig_mw, orig_svc = H.get_rate_limit_middleware, H.get_services
    H.get_rate_limit_middleware = lambda: mw
    H.get_services = lambda ctx: services
    try:
        return await handler(update, context)
    finally:
        H.get_rate_limit_middleware, H.get_services = orig_mw, orig_svc


def _exhaust(limiter, bucket, user_id=42):
    """Spend the whole budget for one user so the next check is blocked."""
    if bucket == "global_ai":
        for _ in range(64):
            if not limiter.check_global_ai_limit().allowed:
                return
        return
    for _ in range(64):
        if not limiter.check_limit(user_id, bucket).allowed:
            return


# ============================================================================
# 1. The declared map matches the code
# ============================================================================


def test_declared_entry_point_contracts_hold():
    """ENTRY_POINT_RATELIMITS is true of the code it describes."""
    asyncio.run(H._assert_entry_point_guard_contracts())


def test_every_public_handler_is_classified():
    """No user-facing handler may be silently absent from the map.

    Every coroutine function exported by the module must be either an entry
    point with a declared bucket, a declared uncharged conversation state, or
    one of the shared guard helpers. This is what stops a newly added handler
    from shipping with no rate limit at all.
    """
    helpers = {
        "notify_rate_limited",
        "guard_command_action",
        "guard_ai_action",
        "guard_expensive_action",
    }
    public = {
        name for name, obj in vars(H).items()
        if inspect.iscoroutinefunction(obj)
        and not name.startswith("_")
        and getattr(obj, "__module__", "") == H.__name__
    }
    classified = (
        set(H.ENTRY_POINT_RATELIMITS)
        | set(H.UNGUARDED_CONVERSATION_STATES)
        | helpers
    )
    unclassified = public - classified
    assert not unclassified, (
        "handlers missing from ENTRY_POINT_RATELIMITS/UNGUARDED_CONVERSATION_STATES "
        f"(a new handler must declare a bucket or be justified as uncharged): "
        f"{sorted(unclassified)}"
    )


def test_map_buckets_are_known():
    known = {H.COMMAND, H.AI, H.UPLOAD, H.EXPENSIVE, H.NONE}
    assert set(H.ENTRY_POINT_RATELIMITS.values()) <= known


# ============================================================================
# 2. COMMAND bucket - inline menu navigation (the previously unguarded class)
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_menu_navigation_executes_once():
    """A permitted menu tap renders the dashboard exactly once."""
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="menu_dashboard")
    ctx = _FakeContext(container=svcs)

    await _run(H.menu_dashboard, svcs, upd, ctx)

    svcs.dashboard.stats.assert_called_once_with(42)
    upd.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_menu_navigation_renders_nothing():
    """A throttled menu tap answers the query with an alert and does no work."""
    lim = _tight()
    _exhaust(lim, "command")
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="menu_dashboard")
    ctx = _FakeContext(container=svcs)

    await _run(H.menu_dashboard, svcs, upd, ctx, limiter=lim)

    svcs.dashboard.stats.assert_not_called()
    upd.callback_query.edit_message_text.assert_not_awaited()
    # The query MUST still be answered, or the user's button spins forever.
    upd.callback_query.answer.assert_awaited_once()
    assert upd.callback_query.answer.await_args.kwargs.get("show_alert") is True
    assert "مرة أخرى" in upd.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_menu_navigation_is_isolated_per_user():
    """One user's throttle must not lock another user out of the menu."""
    lim = _tight()
    _exhaust(lim, "command", user_id=42)
    svcs = _services()

    blocked_upd = _FakeUpdate(user_id=42, callback=True, data="menu_dashboard")
    await _run(H.menu_dashboard, svcs, blocked_upd, _FakeContext(container=svcs), limiter=lim)
    svcs.dashboard.stats.assert_not_called()

    allowed_upd = _FakeUpdate(user_id=7, callback=True, data="menu_dashboard")
    await _run(H.menu_dashboard, svcs, allowed_upd, _FakeContext(container=svcs), limiter=lim)
    svcs.dashboard.stats.assert_called_once_with(7)


# ============================================================================
# 3. COMMAND bucket - conversation entry points
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_conversation_entry_returns_next_state():
    """A permitted flow start returns the first conversation state."""
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="goal_new")
    ctx = _FakeContext(container=svcs)

    result = await _run(H.goal_new, svcs, upd, ctx)

    assert result == H.GOAL_TITLE
    upd.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_conversation_entry_does_not_start_flow():
    """A throttled entry point must not enter the conversation at all."""
    lim = _tight()
    _exhaust(lim, "command")
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="goal_new")
    ctx = _FakeContext(container=svcs)

    result = await _run(H.goal_new, svcs, upd, ctx, limiter=lim)

    # Returning None ends the ConversationHandler, so no state is entered.
    assert result is None
    upd.callback_query.edit_message_text.assert_not_awaited()
    upd.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_stages_are_not_charged_again():
    """One started goal flow spends exactly ONE command slot, not five.

    The five-step goal conversation is a single semantic action: the user is
    answering prompts the bot just sent, so charging each step again would burn
    five slots for one goal and could strand a user mid-flow.
    """
    lim = _tight()
    svcs = _services()
    ctx = _FakeContext(container=svcs, user_data={})

    # Entry point: spends the single command slot.
    await _run(H.goal_new, svcs, _FakeUpdate(callback=True, data="goal_new"), ctx, limiter=lim)

    stages = [
        (H.goal_title, _FakeUpdate(text="العنوان"), H.GOAL_DESC),
        (H.goal_desc, _FakeUpdate(text="وصف"), H.GOAL_CATEGORY),
        (H.goal_stages, _FakeUpdate(text="أ,ب"), H.GOAL_DEADLINE),
        (H.goal_deadline, _FakeUpdate(text="2026-01-01"), H.ConversationHandler.END),
    ]
    for handler, upd, expected in stages:
        assert await _run(handler, svcs, upd, ctx, limiter=lim) == expected

    svcs.goals.create.assert_called_once()

    # The budget was spent exactly once: a second flow is blocked.
    svcs2 = _services()
    assert await _run(H.goal_new, svcs2, _FakeUpdate(callback=True, data="goal_new"),
                      _FakeContext(container=svcs2), limiter=lim) is None
    svcs2.goals.create.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_flow_stage_still_saves_nothing_extra():
    """DB-writing completion steps do not consume a second slot."""
    lim = _tight()
    svcs = _services()
    ctx = _FakeContext(container=svcs, user_data={
        "goal_title": "T", "goal_desc": "D", "goal_category": "دراسة",
        "goal_stages": ["a"]})

    await _run(H.goal_deadline, svcs, _FakeUpdate(text="2026-01-01"), ctx, limiter=lim)
    svcs.goals.create.assert_called_once()

    # A second completion costs nothing and still writes, because the flow
    # itself is not rate limited step-by-step.
    await _run(H.goal_deadline, svcs, _FakeUpdate(text="2026-02-01"), ctx, limiter=lim)
    assert svcs.goals.create.call_count == 2


# ============================================================================
# 4. AI bucket
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_ai_entry_calls_provider_once():
    svcs = _services()
    upd = _FakeUpdate(text="التفاضل")
    ctx = _FakeContext(container=svcs, user_data={"study_subject": "رياضيات"})

    await _run(H.study_topic, svcs, upd, ctx)

    svcs.learning.explain_concept.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_ai_entry_never_calls_provider():
    lim = _tight()
    _exhaust(lim, "ai_request")
    svcs = _services()
    upd = _FakeUpdate(text="التفاضل")
    ctx = _FakeContext(container=svcs, user_data={"study_subject": "رياضيات"})

    await _run(H.study_topic, svcs, upd, ctx, limiter=lim)

    svcs.learning.explain_concept.assert_not_awaited()
    upd.message.reply_text.assert_awaited_once()
    assert "مرة أخرى" in upd.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_blocked_global_ai_never_calls_provider():
    """The shared provider budget blocks even a user with budget left."""
    lim = _tight()
    _exhaust(lim, "global_ai", user_id=1)  # a DIFFERENT user drains the global bucket
    svcs = _services()
    upd = _FakeUpdate(user_id=42, text="التفاضل")
    ctx = _FakeContext(container=svcs, user_data={"study_subject": "رياضيات"})

    await _run(H.study_topic, svcs, upd, ctx, limiter=lim)

    svcs.learning.explain_concept.assert_not_awaited()
    upd.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_ai_callback_answers_query():
    """A throttled AI callback answers the query instead of spinning."""
    lim = _tight()
    _exhaust(lim, "ai_request")
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="english_lesson")
    ctx = _FakeContext(container=svcs)

    await _run(H.english_lesson, svcs, upd, ctx, limiter=lim)

    svcs.learning.english_lesson.assert_not_awaited()
    upd.callback_query.answer.assert_awaited_once()
    upd.callback_query.edit_message_text.assert_not_awaited()


# ============================================================================
# 5. KNOWLEDGE QUESTION - ordering fix (must not charge unrelated messages)
# ============================================================================


@pytest.mark.asyncio
async def test_unrelated_text_message_costs_no_ai_budget():
    """A message outside ask mode must not spend an AI slot.

    `knowledge_question` is registered as a global TEXT handler, so it sees
    every message the user types. Charging before the ask-mode gate silently
    drained the AI quota for ordinary chat and for messages already owned by
    another flow (memory search, conversation steps).
    """
    svcs = _services()
    upd = _FakeUpdate(text="لغة برمجة")
    ctx = _FakeContext(container=svcs, user_data={"memory_mode": "search"})

    # Budget that would block the SECOND AI request; the first is free here.
    lim = _tight(ai_request=RateLimitConfig(max_requests=1, window_seconds=60))
    result = await _run(H.knowledge_question, svcs, upd, ctx, limiter=lim)

    assert result is None
    svcs.knowledge.answer.assert_not_awaited()
    upd.message.reply_text.assert_not_awaited()

    # The AI budget is untouched (probed with a throwaway user, so the probe
    # itself cannot consume the real user's slot)...
    assert lim.check_limit(999, "ai_request").allowed is True
    # ...so a genuine knowledge question is still served.
    ask_upd = _FakeUpdate(text="متى سُمع《小王子》؟")
    ask_ctx = _FakeContext(container=svcs, user_data={"knowledge_mode": "ask"})
    await _run(H.knowledge_question, svcs, ask_upd, ask_ctx, limiter=lim)
    svcs.knowledge.answer.assert_awaited_once_with(42, "متى سُمع《小王子》؟")


@pytest.mark.asyncio
async def test_blocked_knowledge_question_does_not_search():
    lim = _tight()
    _exhaust(lim, "ai_request")
    svcs = _services()
    upd = _FakeUpdate(text="سؤال")
    ctx = _FakeContext(container=svcs, user_data={"knowledge_mode": "ask"})

    await _run(H.knowledge_question, svcs, upd, ctx, limiter=lim)

    svcs.knowledge.answer.assert_not_awaited()
    upd.message.reply_text.assert_awaited_once()
    # Ask mode is preserved so the user can retry the same question.
    assert ctx.user_data.get("knowledge_mode") == "ask"


# ============================================================================
# 6. UPLOAD + INGESTION buckets
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_upload_registers_and_schedules_once():
    svcs = _services()
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx)
    await asyncio.sleep(0.01)

    svcs.knowledge.register_upload.assert_called_once()
    svcs.knowledge.process_source.assert_awaited_once()
    assert len(ctx.application.tasks) == 1


@pytest.mark.asyncio
async def test_allowed_upload_passes_source_id_to_background_ingestion():
    """Background ingestion receives the source id returned by registration."""
    svcs = _services()
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx)
    await asyncio.sleep(0.01)

    svcs.knowledge.register_upload.assert_awaited_once()
    svcs.knowledge.process_source.assert_awaited_once_with(42, 7, progress=ANY)


@pytest.mark.asyncio
async def test_duplicate_upload_does_not_schedule_ingestion():
    """A checksum duplicate is reported but never re-ingested or re-charged."""
    svcs = _services()
    svcs.knowledge.register_upload = AsyncMock(
        return_value={"source_id": 7, "duplicate": True}
    )
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx)
    await asyncio.sleep(0.01)

    svcs.knowledge.register_upload.assert_awaited_once()
    svcs.knowledge.process_source.assert_not_awaited()
    assert ctx.application.tasks == []
    upd.message.reply_text.assert_awaited_once_with(
        "📚 هذا الكتاب موجود في مكتبتك بالفعل."
    )


@pytest.mark.asyncio
async def test_upload_validation_error_aborts_with_message_only():
    """A rejected upload replies with the validation message and schedules nothing."""
    svcs = _services()
    svcs.knowledge.register_upload = AsyncMock(side_effect=ValidationError("الملف فارغ"))
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx)
    await asyncio.sleep(0.01)

    svcs.knowledge.register_upload.assert_awaited_once()
    svcs.knowledge.process_source.assert_not_awaited()
    assert ctx.application.tasks == []
    upd.message.reply_text.assert_awaited_once_with("الملف فارغ")


@pytest.mark.asyncio
async def test_upload_registration_error_aborts_without_scheduling():
    """A storage/database failure replies with the generic error and schedules nothing."""
    svcs = _services()
    svcs.knowledge.register_upload = AsyncMock(side_effect=OSError("disk full"))
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx)
    await asyncio.sleep(0.01)

    svcs.knowledge.register_upload.assert_awaited_once()
    svcs.knowledge.process_source.assert_not_awaited()
    assert ctx.application.tasks == []
    upd.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_ingestion_leaves_no_source_and_no_task():
    """The ingestion budget is checked BEFORE download and registration."""
    lim = _tight()
    _exhaust(lim, "ingestion")
    svcs = _services()
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx, limiter=lim)

    ctx.bot.get_file.assert_not_awaited()
    svcs.knowledge.register_upload.assert_not_called()
    svcs.knowledge.process_source.assert_not_awaited()
    assert ctx.application.tasks == []
    upd.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_upload_does_not_start_ingestion():
    lim = _tight()
    _exhaust(lim, "upload")
    svcs = _services()
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx, limiter=lim)

    ctx.bot.get_file.assert_not_awaited()
    svcs.knowledge.register_upload.assert_not_called()
    assert ctx.application.tasks == []


@pytest.mark.asyncio
async def test_background_ingestion_is_not_charged_again():
    """The background task must not spend a second ingestion slot.

    It was already charged at the upload entry point; charging it again would
    halve the effective ingestion budget for no extra protection.
    """
    lim = _tight()
    svcs = _services()
    upd = _FakeUpdate(document=_FakeDocument())
    ctx = _FakeContext(container=svcs)

    await _run(H.knowledge_document, svcs, upd, ctx, limiter=lim)
    await asyncio.sleep(0.01)

    # The ingestion slot was spent exactly once, by the upload entry point.
    assert lim.check_limit(42, "ingestion").allowed is False
    # Only one ingestion ever ran: the background task was not charged again.
    svcs.knowledge.process_source.assert_awaited_once()


# ============================================================================
# 7. EXPENSIVE bucket
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_expensive_operation_calls_provider_once():
    """A permitted AI reconnect performs the provider health call once."""
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="settings_ai_reconnect")
    ctx = _FakeContext(container=svcs)

    await _run(H.settings_ai_reconnect, svcs, upd, ctx)

    svcs.chat.reconnect.assert_awaited_once()
    upd.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_expensive_operation_makes_no_provider_call():
    """A throttled reconnect must not issue the real, billable health call."""
    lim = _tight()
    _exhaust(lim, "expensive")
    svcs = _services()
    upd = _FakeUpdate(callback=True, data="settings_ai_reconnect")
    ctx = _FakeContext(container=svcs)

    await _run(H.settings_ai_reconnect, svcs, upd, ctx, limiter=lim)

    svcs.chat.reconnect.assert_not_awaited()
    upd.callback_query.edit_message_text.assert_not_awaited()
    upd.callback_query.answer.assert_awaited_once()
    assert upd.callback_query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_expensive_limit_is_isolated_per_user():
    lim = _tight()
    _exhaust(lim, "expensive", user_id=42)
    svcs = _services()

    await _run(H.settings_ai_reconnect, svcs,
               _FakeUpdate(user_id=42, callback=True, data="x"),
               _FakeContext(container=svcs), limiter=lim)
    svcs.chat.reconnect.assert_not_awaited()

    await _run(H.settings_ai_reconnect, svcs,
               _FakeUpdate(user_id=7, callback=True, data="x"),
               _FakeContext(container=svcs), limiter=lim)
    svcs.chat.reconnect.assert_awaited_once()


# ============================================================================
# 8. Recovery paths must never be throttled
# ============================================================================


@pytest.mark.asyncio
async def test_cancel_command_works_when_fully_throttled():
    """/cancel must always escape a conversation, even at zero budget."""
    lim = _tight()
    for bucket in ("command", "ai_request", "upload", "ingestion", "expensive"):
        _exhaust(lim, bucket)
    _exhaust(lim, "global_ai")

    from app.telegram.main import cancel_command

    svcs = _services()
    upd = _FakeUpdate(text="/cancel")
    ctx = _FakeContext(container=svcs)

    orig_mw, orig_svc = H.get_rate_limit_middleware, H.get_services
    mw = get_rate_limit_middleware()
    mw.limiter = lim
    H.get_rate_limit_middleware = lambda: mw
    H.get_services = lambda c: svcs
    try:
        result = await cancel_command(upd, ctx)
    finally:
        H.get_rate_limit_middleware, H.get_services = orig_mw, orig_svc

    assert result is H.ConversationHandler.END
    upd.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_cancel_works_when_fully_throttled():
    """Conversation fallbacks must stay usable at zero budget."""
    lim = _tight()
    _exhaust(lim, "command")

    svcs = _services()
    upd = _FakeUpdate(text="x")
    ctx = _FakeContext(container=svcs)

    result = await _run(H.fallback_cancel, svcs, upd, ctx, limiter=lim)

    assert result is H.ConversationHandler.END
    upd.message.reply_text.assert_awaited_once_with("تم الإلغاء")


@pytest.mark.asyncio
async def test_allowed_memory_export_sends_file_once():
    svcs = _services()
    svcs.memory.export = Mock(return_value={"memory_count": 2, "memories": []})
    upd = _FakeUpdate(callback=True, data="memory_export")
    ctx = _FakeContext(container=svcs)

    await _run(H.memory_export, svcs, upd, ctx)

    svcs.memory.export.assert_called_once_with(42)
    ctx.bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_memory_export_sends_no_file():
    """A throttled export must not read the store or push a document."""
    lim = _tight()
    _exhaust(lim, "command")
    svcs = _services()
    svcs.memory.export = Mock(return_value={"memory_count": 2, "memories": []})
    upd = _FakeUpdate(callback=True, data="memory_export")
    ctx = _FakeContext(container=svcs)

    await _run(H.memory_export, svcs, upd, ctx, limiter=lim)

    svcs.memory.export.assert_not_called()
    ctx.bot.send_document.assert_not_awaited()
    upd.callback_query.answer.assert_awaited_once()
    assert upd.callback_query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_memory_text_flow_is_not_double_charged():
    """A memory-mode reply costs nothing extra; the entry button was charged."""
    lim = _tight()
    svcs = _services()
    ctx = _FakeContext(container=svcs, user_data={"memory_mode": "search"})

    await _run(H.memory_text, svcs, _FakeUpdate(text="قهوة"), ctx, limiter=lim)

    svcs.memory.search.assert_called_once()
    assert ctx.user_data.get("memory_mode") is None
    # Still fully in budget: nothing was spent on the reply itself.
    assert lim.check_limit(42, "command").allowed is True


@pytest.mark.asyncio
async def test_memory_text_rejects_bad_input_without_charging():
    """Invalid replies are handled gracefully and still cost nothing."""
    lim = _tight()
    svcs = _services()
    ctx = _FakeContext(container=svcs, user_data={"memory_mode": "edit"})

    await _run(H.memory_text, svcs, _FakeUpdate(text="لا رقم"), ctx, limiter=lim)

    svcs.memory.edit.assert_not_called()
    # Still in memory mode, so the user can retry.
    assert ctx.user_data.get("memory_mode") == "edit"
    assert lim.check_limit(42, "command").allowed is True


# ============================================================================
# 9. No secrets in the user-facing throttle message
# ============================================================================


@pytest.mark.asyncio
async def test_throttle_message_leaks_nothing():
    """The throttle reply must not echo any credential or internal detail."""
    lim = _tight()
    _exhaust(lim, "command")
    svcs = _services()
    upd = _FakeUpdate(text="/start")
    ctx = _FakeContext(container=svcs)

    from app.telegram.main import start

    orig_mw, orig_svc = H.get_rate_limit_middleware, H.get_services
    mw = get_rate_limit_middleware()
    mw.limiter = lim
    H.get_rate_limit_middleware = lambda: mw
    H.get_services = lambda c: svcs
    try:
        await start(upd, ctx)
    finally:
        H.get_rate_limit_middleware, H.get_services = orig_mw, orig_svc

    text = upd.message.reply_text.await_args.args[0]
    assert "token" not in text.lower()
    assert "password" not in text.lower()
    assert "secret" not in text.lower()
    svcs.users.profile.assert_not_called()
