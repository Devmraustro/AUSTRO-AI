"""Regression tests for the failures users actually saw in Telegram.

Two incidents are locked down here.

LEARN-1 (from the user's screenshots, 2026-11-09)
    Tapping "ابدأ الدرس" after creating a learning goal answered with the
    generic "❌ عذراً، حدث خطأ" bubble (three times in a row). The cause was
    ``AdaptiveLearningEngine.__init__`` assigning ``self.curricula`` (the
    ``CurriculumEngine``), which shadowed the facade method
    ``curricula(owner_user_id, goal_id)``. Every call raised
    ``TypeError: 'CurriculumEngine' object is not callable``, the exception
    escaped the handler and the global error handler replied with the safe
    generic message.

    The same tests prove the whole flow now works offline end to end: goal ->
    objectives -> curriculum -> lesson -> quick check.

RENDER-1
    A single unbalanced ``*``/``_`` in AI output made Telegram reject the whole
    message ("Can't parse entities"), so the user lost the answer and got the
    error bubble. ``app.telegram.rendering`` now retries without formatting and
    chunks oversized answers instead of failing.

FINISH-1
    "✅ أكملت مهمة" (plan_complete), "🎯 تحدي اليوم" (discipline_challenge),
    "📚 جلسة دراسة" (study_session), "📖 شرح مفهوم برمجي" (prog_concept) and
    "📊 إحصائيات مفصلة" (progress_stats) were rendered as buttons with no
    handler at all - tapping them did nothing. Each one now has a handler and
    a test.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-learning-flow-token")
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["DB_PATH"] = str(
    pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_learning_flow.db"
)

import pytest

from app.domain.entities import NewPlan
from app.learning.engine import AdaptiveLearningEngine
from app.telegram import handlers as H
from app.telegram import rendering as render

OWNER = 4242


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, user_id=OWNER):
        self.id = user_id
        self.username = "learner"
        self.first_name = "Learner"
        self.last_name = ""


class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.chat_id = 111
        self.reply_text = AsyncMock(return_value=None)


class _FakeCallbackQuery:
    def __init__(self, data="x"):
        self.data = data
        self.message = _FakeMessage()
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUpdate:
    def __init__(self, text="", data="x", callback=False, user_id=OWNER):
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = MagicMock(id=111)
        if callback:
            self.callback_query = _FakeCallbackQuery(data)
            self.message = None
        else:
            self.callback_query = None
            self.message = _FakeMessage(text)


class _FakeContext:
    def __init__(self, container, user_data=None):
        self.bot_data = {"container": container}
        self.user_data = dict(user_data or {})
        self.chat_data = {}
        self.application = MagicMock()
        self.application.create_task = MagicMock()

def _container():
    from app.core.container import build_container

    return build_container()


def _rendered(update: _FakeUpdate) -> str:
    """All text passed to the Telegram API for this update."""
    parts = []
    if update.callback_query is not None:
        for call in update.callback_query.edit_message_text.call_args_list:
            parts.append(call.args[0] if call.args else "")
        for call in update.callback_query.message.reply_text.call_args_list:
            parts.append(call.args[0] if call.args else "")
    if update.message is not None:
        for call in update.message.reply_text.call_args_list:
            parts.append(call.args[0] if call.args else "")
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def wide_rate_limits(monkeypatch):
    """These tests are about behaviour, not about spending the budget."""
    from app.security.rate_limiter import (
        DEFAULT_LIMITS,
        RateLimitConfig,
        RateLimiter,
        get_rate_limit_middleware,
    )

    wide = {name: RateLimitConfig(max_requests=10_000, window_seconds=60)
            for name in DEFAULT_LIMITS}
    monkeypatch.setattr(get_rate_limit_middleware(), "limiter",
                        RateLimiter(limits=wide))
    yield


def _seed_learning_goal(container, owner=OWNER, title="تعلم اللغة الإنجليزية",
                        with_curriculum=True):
    engine = container.learning_engine
    chain = engine.create_goal_chain(owner, vision=f"رؤيتي: {title}", long_term=title)
    goal_id = chain["long_term_goal_id"]
    for line in ("أساسيات القواعد", "المفردات", "المحادثة"):
        engine.add_objective(owner, goal_id, title=line)
    if with_curriculum:
        engine.build_curriculum(owner, goal_id, title=title, mode="READ")
    return goal_id


# ---------------------------------------------------------------------------
# LEARN-1: the engine's facade methods are not shadowed by its sub-engines
# ---------------------------------------------------------------------------


def test_no_instance_attribute_shadows_a_facade_method():
    """A sub-engine assigned to an instance attribute hides a same-named method.

    This is the general guard for the class of bug that produced LEARN-1: it
    re-derives the shadowing set from the source instead of trusting a list.
    """
    container = _container()
    engine = container.learning_engine
    source = inspect.getsource(AdaptiveLearningEngine.__init__)
    assigned = set(re.findall(r"self\.(\w+)\s*=(?!=)", source))
    methods = {
        name for name, member in inspect.getmembers(
            AdaptiveLearningEngine, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    shadowed = sorted(assigned & methods)
    assert not shadowed, (
        "these engine attributes shadow facade methods and make them "
        f"uncallable: {shadowed}"
    )
    # The facade method really is callable and returns a list.
    goal_id = _seed_learning_goal(container)
    assert isinstance(engine.curricula(OWNER, goal_id), list)


def test_sub_engines_are_only_exposed_under_the_suffixed_names():
    """The facade is the public API; aliases are what created LEARN-1.

    Keeping `engine.sessions` around as a convenience alias is exactly how the
    shadowing bug stayed invisible: the attribute existed, so nothing failed
    until the day a facade method needed that name.
    """
    engine = _container().learning_engine
    aliases = ("curricula", "scheduler", "sessions", "lessons", "assessments",
               "misconceptions", "diagnostic", "planner", "review", "coach",
               "books", "flashcards")
    leaked = [name for name in aliases if name in vars(engine)]
    assert not leaked, f"sub-engines leak under bare names: {leaked}"
    for name in aliases:
        assert hasattr(engine, f"{name}_"), f"missing sub-engine {name}_"


def test_facade_curricula_method_exists_and_works():
    container = _container()
    engine = container.learning_engine
    goal_id = _seed_learning_goal(container)
    engine.build_curriculum(OWNER, goal_id, title="تعلم اللغة الإنجليزية")
    curricula = engine.curricula(OWNER, goal_id)
    assert curricula and curricula[0]["curriculum_id"]
    assert engine.curriculum(OWNER, curricula[0]["curriculum_id"]) is not None


# ---------------------------------------------------------------------------
# LEARN-1: the user-visible flow from the screenshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learning_hub_opens_without_error():
    container = _container()
    _seed_learning_goal(container)
    update = _FakeUpdate(callback=True, data="menu_learn")
    context = _FakeContext(container)

    await H.menu_learn(update, context)

    text = _rendered(update)
    assert "رحلة التعلم" in text
    assert "حدث خطأ" not in text
    assert "أساسيات القواعد" not in text  # overview, not the lesson
    update.callback_query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_next_lesson_renders_instead_of_the_error_bubble():
    """The regression: this is the button that showed "حدث خطأ" three times."""
    container = _container()
    _seed_learning_goal(container)
    update = _FakeUpdate(callback=True, data="menu_learn_next")
    context = _FakeContext(container)

    await H.menu_learn_next(update, context)

    text = _rendered(update)
    assert "حدث خطأ" not in text, "the generic error bubble is back"
    assert "📖" in text
    assert "درس" in text
    # the session state is ready for the quiz button
    state = context.user_data.get("learn_session_state") or {}
    assert state.get("objective_id")


@pytest.mark.asyncio
async def test_next_lesson_survives_a_failing_coach_read():
    """A degraded overview must never cost the user the lesson."""
    container = _container()
    _seed_learning_goal(container)
    engine = container.learning_engine
    engine.coach_today = MagicMock(side_effect=RuntimeError("coach down"))
    update = _FakeUpdate(callback=True, data="menu_learn")
    context = _FakeContext(container)

    await H.menu_learn(update, context)

    text = _rendered(update)
    assert "رحلة التعلم" in text
    assert "حدث خطأ" not in text


@pytest.mark.asyncio
async def test_quick_check_flow_grades_all_answers():
    container = _container()
    _seed_learning_goal(container)
    services = container

    lesson_update = _FakeUpdate(callback=True, data="menu_learn_next")
    context = _FakeContext(container)
    await H.menu_learn_next(lesson_update, context)

    quiz_update = _FakeUpdate(callback=True, data="menu_learn_quiz")
    await H.menu_learn_quiz(quiz_update, context)
    questions = (context.user_data.get("learn_quiz") or {}).get("questions") or []
    assert questions, "quick check produced no questions"

    # Answer the first option of every question.
    for index, question in enumerate(questions):
        if not question.get("options"):
            continue
        answer_update = _FakeUpdate(callback=True, data=f"learn_q_{index}_0")
        await H.learn_quiz_btn(answer_update, context)

    assert context.user_data.get("learn_quiz") is None, "quiz was not graded"
    assert "نتيجة" in _rendered(answer_update)
    assert services.learning_engine.assessment is not None


@pytest.mark.asyncio
async def test_objectives_with_bullets_are_parsed_and_created():
    container = _container()
    update = _FakeUpdate(text="1. المتغيرات\n- الحلقات\n• الدوال\n\nالمتغيرات")
    context = _FakeContext(container, user_data={"learn_pending_title": "بايثون"})

    result = await H.learn_objectives_text(update, context)

    from telegram.ext import ConversationHandler

    assert result == ConversationHandler.END
    text = _rendered(update)
    assert "حدث خطأ" not in text
    assert "أُنشئ الهدف" in text
    goals = container.learning_engine.goals(OWNER)
    long_term = [goal for goal in goals if goal.get("kind") == "long_term"]
    assert long_term and long_term[0]["title"] == "بايثون"
    objectives = container.learning_engine.objectives(
        OWNER, long_term[0]["goal_id"])
    assert [objective["title"] for objective in objectives] == [
        "المتغيرات", "الحلقات", "الدوال",
    ]


@pytest.mark.asyncio
async def test_empty_objectives_keep_the_conversation_open():
    container = _container()
    update = _FakeUpdate(text="   ")
    context = _FakeContext(container, user_data={"learn_pending_title": "بايثون"})

    result = await H.learn_objectives_text(update, context)

    assert result == H.LEARN_GOAL_OBJECTIVES, "the user cannot retry"
    assert "لم أجد أي هدف" in _rendered(update)


@pytest.mark.asyncio
async def test_goal_is_kept_when_curriculum_build_fails(monkeypatch):
    container = _container()
    engine = container.learning_engine
    monkeypatch.setattr(engine, "build_curriculum",
                        MagicMock(side_effect=RuntimeError("boom")))
    update = _FakeUpdate(text="المتغيرات\nالحلقات")
    context = _FakeContext(container, user_data={"learn_pending_title": "بايثون"})

    await H.learn_objectives_text(update, context)

    text = _rendered(update)
    assert "حدث خطأ" not in text
    assert "تعذر تجهيز المنهج" in text
    # the user's work survived the failure
    goals = [goal for goal in engine.goals(OWNER) if goal.get("kind") == "long_term"]
    assert goals


# ---------------------------------------------------------------------------
# RENDER-1: a bad markup character can no longer lose the message
# ---------------------------------------------------------------------------


class _RejectingCall:
    """A send/edit call that fails on formatted text, like Telegram does."""

    def __init__(self, reject_when=lambda kwargs: "parse_mode" in kwargs):
        self.calls = []
        self._reject = reject_when

    async def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self._reject(kwargs):
            from telegram.error import BadRequest

            raise BadRequest("Can't parse entities: can't find end of the entity")
        return "sent"


class _Target:
    def __init__(self, method, call):
        setattr(self, method, call)


@pytest.mark.asyncio
async def test_markdown_failure_falls_back_to_plain_text():
    call = _RejectingCall()
    message = _Target("reply_text", call)

    await render.reply(message, "**نصيحة** _غير متوازنة", parse_mode="Markdown")

    assert len(call.calls) == 2, "the fallback attempt is missing"
    assert call.calls[0][1].get("parse_mode") == "Markdown"
    assert "parse_mode" not in call.calls[1][1], "fallback must be plain text"
    assert "نصيحة" in call.calls[1][0], "the content was lost"


@pytest.mark.asyncio
async def test_unbalanced_markers_are_repaired_before_sending():
    assert render.normalize_markup("**bold*") == "**bold"
    assert render.normalize_markup("2 * 3 = 6") == "2  3 = 6"
    # An ESCAPED underscore is valid markup and must be preserved.
    assert render.normalize_markup("@Mr\\_Austro") == "@Mr\\_Austro"


@pytest.mark.asyncio
async def test_long_answer_is_chunked_instead_of_failing():
    call = _RejectingCall(reject_when=lambda kwargs: False)
    message = _Target("reply_text", call)
    body = "\n\n".join(f"فقرة {index} " + "نص " * 60 for index in range(40))
    assert len(body) > render.MAX_MESSAGE_LENGTH

    await render.reply(message, body)

    assert len(call.calls) > 1
    assert all(len(text) <= render.MAX_MESSAGE_LENGTH for text, _ in call.calls)
    # only the last chunk carries the keyboard / no continuation markers left
    assert render.CONTINUATION_MARKER.strip() not in call.calls[-1][0]


@pytest.mark.asyncio
async def test_identical_edit_is_not_reported_as_an_error():
    async def edit(text, **kwargs):
        from telegram.error import BadRequest

        raise BadRequest("Message is not modified")

    query = _Target("edit_message_text", edit)
    query.message = _Target("reply_text", _RejectingCall())

    assert await render.edit(query, "نفس النص") is None


@pytest.mark.asyncio
async def test_edit_falls_back_to_a_new_message_when_editing_fails():
    async def edit(text, **kwargs):
        from telegram.error import BadRequest

        raise BadRequest("Message to edit not found")

    new_call = _RejectingCall(reject_when=lambda kwargs: False)
    query = _Target("edit_message_text", edit)
    query.message = _Target("reply_text", new_call)

    await render.edit(query, "محتوى جديد")

    assert new_call.calls, "the content was lost instead of re-sent"
    assert "محتوى جديد" in new_call.calls[0][0]


# ---------------------------------------------------------------------------
# FINISH-1: every rendered button has a handler, and it works
# ---------------------------------------------------------------------------


def _registered_patterns() -> list:
    patterns = []
    for handler in H.get_menu_handlers():
        patterns.append((handler.pattern, handler.callback.__name__))
    for factory in (
        H.get_registration_handlers, H.get_learning_handlers, H.get_goal_handlers,
        H.get_plan_handlers, H.get_study_handlers, H.get_english_handlers,
        H.get_programming_handlers, H.get_accountability_handlers,
        H.get_habit_handlers, H.get_reminder_handlers,
    ):
        conversation = factory()
        for entry in conversation.entry_points:
            patterns.append((getattr(entry, "pattern", None),
                             getattr(entry.callback, "__name__", "?")))
        for handlers in conversation.states.values():
            for handler in handlers:
                if hasattr(handler, "pattern"):
                    patterns.append((handler.pattern,
                                     getattr(handler.callback, "__name__", "?")))
    return patterns


def test_every_button_in_the_bot_has_a_handler():
    """Catches the class of bug behind the dead buttons.

    Buttons are collected from the module source (including f-strings, which
    are reduced to their literal prefix) and matched against the registered
    callback patterns the same way PTB matches them.
    """
    source = pathlib.Path(H.__file__).read_text()
    buttons = set()
    for match in re.finditer(r'callback_data=f?"([^"]+)"', source):
        data = match.group(1)
        # f-string buttons: a placeholder always resolves to a number here
        # (`learn_q_{qi}_{oi}`, `plan_task_{index}`, ...).
        data = re.sub(r"\{[^}]*\}", "0", data)
        buttons.add(data)

    patterns = [pattern for pattern, _ in _registered_patterns() if pattern]
    dead = sorted(
        data for data in buttons
        if not any(re.match(pattern, data) for pattern in patterns)
    )
    assert not dead, f"buttons rendered with no handler (taps do nothing): {dead}"


def _plan_owner(container):
    from app.domain.context import UserContext

    container.users.ensure_user(UserContext(OWNER, "learner", "Learner", ""))


def _today() -> str:
    from datetime import datetime

    return datetime.utcnow().strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_plan_task_completion_updates_progress():
    container = _container()
    _plan_owner(container)
    today = _today()
    container.plans.create(NewPlan(
        user_id=OWNER, date=today,
        tasks=[{"task": "قراءة 10 صفحات", "done": False},
               {"task": "مراجعة المفردات", "done": False}],
        priorities=["قراءة"], review_time="20:00", break_time="10 دقائق",
    ))

    update = _FakeUpdate(callback=True, data="plan_task_0")
    await H.plan_complete_task(update, _FakeContext(container))

    text = _rendered(update)
    assert "حدث خطأ" not in text
    assert "1/2" in text and "50%" in text
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 1
    assert "✅" in update.callback_query.answer.call_args.args[0]

    # The button now asks for the opposite state explicitly.
    reopened = _FakeUpdate(callback=True, data="plan_task_0_0")
    await H.plan_complete_task(reopened, _FakeContext(container))
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 0
    assert "0/2" in _rendered(reopened)


@pytest.mark.asyncio
async def test_repeated_delivery_of_one_completion_is_idempotent():
    """A THIRD party retry must not undo the completion.

    This is the exact failure found while transferring the fixes: the same
    callback delivered twice (Telegram retry / two workers) used to flip the
    task back, so `weekly_tasks` went 1 -> 0 and the user's completion was
    silently lost.
    """
    container = _container()
    _plan_owner(container)
    today = _today()
    container.plans.create(NewPlan(
        user_id=OWNER, date=today, tasks=[{"task": "مهمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))

    first = _FakeUpdate(callback=True, data="plan_task_0")
    await H.plan_complete_task(first, _FakeContext(container))
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 1

    for _ in range(3):  # the same callback, delivered again
        again = _FakeUpdate(callback=True, data="plan_task_0")
        await H.plan_complete_task(again, _FakeContext(container))
        assert container.dashboard.stats(OWNER)["weekly_tasks"] == 1, (
            "a retried completion must never be counted twice or undone"
        )
        assert "1/1" in _rendered(again)
        assert "بالفعل" in again.callback_query.answer.call_args.args[0]

    plan = container.plans.for_date(OWNER, today)
    assert plan["completed_tasks"] == [True]
    assert plan["status"] == "completed"


@pytest.mark.asyncio
async def test_repeated_delivery_of_one_reopen_is_idempotent():
    container = _container()
    _plan_owner(container)
    today = _today()
    container.plans.create(NewPlan(
        user_id=OWNER, date=today, tasks=[{"task": "مهمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))
    container.plans.set_task_done(OWNER, today, 0, True)
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 1

    for _ in range(3):
        update = _FakeUpdate(callback=True, data="plan_task_0_0")
        await H.plan_complete_task(update, _FakeContext(container))
        assert container.dashboard.stats(OWNER)["weekly_tasks"] == 0, (
            "re-opening must subtract exactly one completion"
        )
    assert container.plans.for_date(OWNER, today)["completed_tasks"] == [False]


def test_service_counts_only_real_transitions():
    """Statistics come from the transition status, never from the request."""
    container = _container()
    _plan_owner(container)
    today = _today()
    container.plans.create(NewPlan(
        user_id=OWNER, date=today,
        tasks=[{"task": "أ"}, {"task": "ب"}],
        priorities=[], review_time="20:00", break_time="10",
    ))
    service = container.plans

    assert service.set_task_done(OWNER, today, 0, True)["transition"] == "changed"
    assert service.set_task_done(OWNER, today, 0, True)["transition"] == "already"
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 1

    assert service.set_task_done(OWNER, today, 1, True)["transition"] == "changed"
    assert container.dashboard.stats(OWNER)["weekly_tasks"] == 2
    plan = service.for_date(OWNER, today)
    assert service.all_done(plan) is True


def test_completion_credits_the_plans_own_date_not_today():
    """Completing yesterday's plan must not rewrite today's progress row."""
    container = _container()
    _plan_owner(container)
    old_date = "2026-01-05"
    container.plans.create(NewPlan(
        user_id=OWNER, date=old_date, tasks=[{"task": "قديمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))

    container.plans.set_task_done(OWNER, old_date, 0, True)

    row = [r for r in database_progress(container, OWNER) if r["date"] == old_date]
    assert row and row[0]["tasks_completed"] == 1


def database_progress(container, user_id):
    return container.db.get_progress(user_id, days=3650)


def test_task_state_is_scoped_to_the_owning_user():
    """A stale callback from another account must never touch this plan."""
    container = _container()
    _plan_owner(container)
    other = OWNER + 1
    from app.domain.context import UserContext

    container.users.ensure_user(UserContext(other, "other", "Other", ""))
    today = _today()
    container.plans.create(NewPlan(
        user_id=OWNER, date=today, tasks=[{"task": "خاصة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))

    assert container.plans.set_task_done(other, today, 0, True) is None
    assert container.plans.for_date(OWNER, today)["completed_tasks"] in (None, [])
    assert container.dashboard.stats(other)["weekly_tasks"] == 0


@pytest.mark.asyncio
async def test_plan_task_out_of_range_is_rejected_without_a_crash():
    container = _container()
    container.users.ensure_user(
        __import__("app.domain.context", fromlist=["UserContext"]).UserContext(
            OWNER, "learner", "Learner", ""))
    today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
    container.plans.create(NewPlan(
        user_id=OWNER, date=today, tasks=[{"task": "مهمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))
    update = _FakeUpdate(callback=True, data="plan_task_9")

    await H.plan_complete_task(update, _FakeContext(container))

    assert update.callback_query.answer.call_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_discipline_challenge_uses_the_users_real_state():
    container = _container()
    container.users.ensure_user(
        __import__("app.domain.context", fromlist=["UserContext"]).UserContext(
            OWNER, "learner", "Learner", ""))
    today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
    container.plans.create(NewPlan(
        user_id=OWNER, date=today, tasks=[{"task": "كتابة 300 كلمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))
    update = _FakeUpdate(callback=True, data="discipline_challenge")

    await H.discipline_challenge(update, _FakeContext(container))

    text = _rendered(update)
    assert "تحدي اليوم" in text
    assert "كتابة 300 كلمة" in text


@pytest.mark.asyncio
async def test_progress_stats_renders_real_numbers():
    container = _container()
    container.users.ensure_user(
        __import__("app.domain.context", fromlist=["UserContext"]).UserContext(
            OWNER, "learner", "Learner", ""))
    update = _FakeUpdate(callback=True, data="progress_stats")

    await H.progress_stats(update, _FakeContext(container))

    text = _rendered(update)
    assert "إحصائيات مفصلة" in text
    assert "حدث خطأ" not in text


def test_new_entry_points_declare_a_rate_limit_bucket():
    for name in ("plan_complete_task", "progress_stats", "discipline_challenge",
                 "prog_concept", "study_session", "study_session_topic"):
        assert name in H.ENTRY_POINT_RATELIMITS, name


def test_text_router_targets_are_real_coroutines():
    """Every mode the router dispatches to must be a guard-declared coroutine.

    `text_router` is the only global TEXT handler, so these targets are not
    registered in the application graph themselves - they are reached through
    the router and therefore only need to exist, be awaitable, and be declared
    in the rate-limit map.
    """
    for target in ("study_session_topic", "prog_concept_text"):
        func = getattr(H, target, None)
        assert func is not None, f"text_router calls a missing {target}"
        assert inspect.iscoroutinefunction(func), f"{target} is not awaitable"
        assert target in H.ENTRY_POINT_RATELIMITS, target


# ---------------------------------------------------------------------------
# PostgreSQL parity for the completion path
# ---------------------------------------------------------------------------


def test_scalar_max_is_translated_for_postgresql():
    """`MAX(a, b)` is SQLite-only; PostgreSQL's max() is an aggregate.

    The reopen path clamps with `MAX(tasks_completed + ?, 0)`. Untranslated it
    fails on PostgreSQL with "function max(integer, integer) does not exist" -
    a bug SQLite cannot reveal. `translate_query` is exactly what the
    PostgreSQL cursor applies, so asserting on it is the offline proxy for the
    integration test that needs a live server.
    """
    from app.database.dialect import translate_query

    sql = ("UPDATE progress SET tasks_completed = "
           "MAX(COALESCE(tasks_completed, 0) + ?, 0) "
           "WHERE user_id = ? AND date = ?")
    out, _ = translate_query(sql)

    assert "GREATEST(COALESCE(tasks_completed, 0) + %s, 0)" in out
    assert "MAX(" not in out.upper()
    assert "?" not in out, "bind placeholders must be %s for psycopg2"


def test_aggregates_are_never_rewritten_to_greatest():
    """Only the two-argument scalar form is rewritten."""
    from app.database.dialect import translate_query

    for sql in (
        "SELECT COALESCE(SUM(tasks_completed), 0) FROM progress",
        "SELECT MAX(count) FROM learning_misconceptions",
        "SELECT MAX(a, b, c) FROM t",
    ):
        out, _ = translate_query(sql)
        assert "GREATEST" not in out, f"aggregate rewritten: {out}"


def test_every_plan_sql_statement_survives_the_postgres_translation():
    """No repository statement may keep a `?` placeholder on PostgreSQL."""
    import pathlib as _pathlib
    import re as _re

    from app.database.dialect import translate_query

    source = _pathlib.Path(
        __import__("app.database.repositories", fromlist=["x"]).__file__
    ).read_text()
    executed = _re.findall(r'cursor\.execute\(\s*\n?\s*((?:"[^"]*"\s*\n?\s*)+)', source)
    statements = [" ".join("".join(_re.findall(r'"([^"]*)"', chunk)).split())
                  for chunk in executed]
    relevant = [sql for sql in statements
                if "daily_plans" in sql or "tasks_completed" in sql]
    assert len(relevant) >= 7, "the completion SQL was not found in the source"
    for sql in relevant:
        out, _ = translate_query(sql)
        assert "?" not in out, f"untranslated placeholder in: {sql}"
        assert "MAX(" not in out.upper() or "SELECT MAX" in out.upper(), (
            f"scalar MAX() left untranslated: {sql}"
        )


# ---------------------------------------------------------------------------
# Found by replaying the whole menu graph end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prog_concept_from_a_typed_message_does_not_crash():
    """text_router feeds prog_concept_text a MESSAGE update, not a callback.

    The first version of this handler re-entered `prog_concept`, which assumed
    `update.callback_query` was present and raised AttributeError on the text
    path - the button worked, typing the concept did not.
    """
    container = _container()
    context = _FakeContext(container, user_data={"prog_concept_mode": True})
    update = _FakeUpdate(text="الوراثة في بايثون")
    object.__setattr__(update, "callback_query", None)

    await H.prog_concept_text(update, context)

    text = _rendered(update)
    assert "حدث خطأ" not in text
    assert text.strip(), "the concept explanation was not sent"
    assert context.user_data.get("prog_concept_mode") is None


@pytest.mark.asyncio
async def test_prog_concept_button_still_works():
    container = _container()
    context = _FakeContext(container, user_data={"prog_concept_topic": "Big-O"})
    update = _FakeUpdate(callback=True, data="prog_concept")

    await H.prog_concept(update, context)

    assert _rendered(update).strip()
    update.callback_query.answer.assert_awaited()


def test_study_session_has_a_deterministic_local_generator():
    """Every AI capability the UI can reach needs an offline fallback."""
    from app.ai.local import LocalProvider
    from app.domain.ai import AIRequest

    out = LocalProvider().generate(AIRequest(
        capability="study_session", prompt="ignored", data={"topic": "التشفير"},
    ))
    assert "جلسة دراسة" in out
    assert "التشفير" in out
    assert "دقائق" in out
