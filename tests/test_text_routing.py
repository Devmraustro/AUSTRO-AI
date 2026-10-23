"""AUSTRO AI - plain-text dispatch and /cancel routing regressions.

Two production defects are locked down here.

TEXT-1 (functional, user-visible)
    `build_application()` registered two global handlers for
    `filters.TEXT & ~filters.COMMAND`: the knowledge question handler first and
    the memory text handler second. PTB stops at the first handler whose
    `check_update` matches and never offers the update to later handlers, so
    the memory handler was unreachable. `knowledge_question` returned early
    whenever ask-mode was off, so the memory search / edit / forget flows
    answered the user's text with nothing at all - including "forget", which is
    a privacy control.

    Fix: exactly one global TEXT handler (`text_router`) that dispatches by the
    flow that is waiting for the text. The tests below fail if a second global
    TEXT handler is ever registered again.

CANCEL-1 (functional, user-visible)
    `/cancel` was registered as a top-level CommandHandler BEFORE the
    ConversationHandlers. The first match wins, so the global handler answered
    every /cancel and the conversations' `fallback_cancel` - the only code that
    actually clears the conversation state - never ran. The user was told
    "cancelled" while the conversation stayed active and the next message was
    still consumed as an answer.

    Fix: `/cancel` is registered after every ConversationHandler. The tests
    below drive the real `build_application()` handler graph.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKLMnoPQRstuvwxyz-DummyToken")
os.environ["GEMINI_API_KEY"] = ""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler, filters

from app.security.rate_limiter import (
    DEFAULT_LIMITS,
    RateLimitConfig,
    RateLimiter,
    get_rate_limit_middleware,
)
from app.telegram import handlers as H
from app.telegram import main as M


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Svc:
    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, fn)


def _memory_services(**overrides):
    base = dict(
        memory=_Svc(
            search=Mock(return_value=[]),
            edit=Mock(return_value=True),
            forget=Mock(return_value=True),
            clear=Mock(return_value=3),
        ),
        knowledge=_Svc(answer=AsyncMock(return_value=_Svc(text="إجابة", citations=[]))),
    )
    base.update(overrides)
    return _Svc(**base)


class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.reply_text = AsyncMock(return_value=None)


class _FakeUser:
    def __init__(self, user_id=42):
        self.id = user_id


class _FakeUpdate:
    """Minimal update: a plain-text message from one user."""

    def __init__(self, text, user_id=42):
        self.effective_user = _FakeUser(user_id)
        self.message = _FakeMessage(text)
        self.callback_query = None
        self.channel_post = None
        self.edited_channel_post = None


class _FakeContext:
    def __init__(self, container, user_data=None):
        self.bot_data = {"container": container}
        self.user_data = dict(user_data or {})


def _real_update(text="/cancel", user_id=42, bot=None):
    """A real-shaped update.

    PTB's `filters.COMMAND` keys off the BOT_COMMAND entity that Telegram
    attaches to a command message, not off the leading slash, so the entity has
    to be present or `/cancel` would be classified as plain text.
    """
    user = User(id=user_id, first_name="T", is_bot=False)
    chat = Chat(id=user_id, type="private")
    entities = None
    if text.startswith("/"):
        entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))]
    message = Message(
        message_id=1, date=datetime.now(), chat=chat, from_user=user,
        text=text, entities=entities,
    )
    update = Update(update_id=1, message=message)
    if bot is not None:
        # CommandHandler.check_update inspects the bot (for @botname commands),
        # so a real-shaped update needs the application's bot attached both to
        # the update and to the message it wraps.
        message.set_bot(bot)
        update.set_bot(bot)
    return update


def _fake_bot(app):
    """Give the application a bot identity without any network call.

    `CommandHandler.check_update` compares the command against
    `bot.username`, and `Bot.username` raises until the bot is initialized
    (which would call getMe). Supplying the cached bot user is the supported
    way to unit-test handler dispatch offline.
    """
    if app.bot._bot_user is None:
        app.bot._bot_user = User(
            id=777000, first_name="AUSTRO", is_bot=True, username="austro_ai_bot"
        )
    return app.bot


def _global_text_handlers(app):
    """Every handler in the default group that claims any plain text."""
    return [
        handler
        for handler in app.handlers[0]
        if isinstance(handler, MessageHandler)
        and handler.filters is not None
        and handler.filters.check_update(_real_update("مرحبا"))
    ]


def _first_matching(app, update):
    for handler in app.handlers[0]:
        if handler.check_update(update) not in (None, False):
            return handler
    return None


# ---------------------------------------------------------------------------
# TEXT-1: a single global text handler, and it dispatches correctly
# ---------------------------------------------------------------------------


def test_exactly_one_global_text_handler_is_registered():
    """A second global TEXT handler would be unreachable, so forbid it."""
    app = M.build_application()
    text_handlers = _global_text_handlers(app)
    assert len(text_handlers) == 1, (
        "PTB only dispatches to the first matching handler; extra global TEXT "
        f"handlers are dead code: {[h.callback.__name__ for h in text_handlers]}"
    )
    assert text_handlers[0].callback is H.text_router


async def test_memory_search_flow_receives_the_typed_text():
    """The regression: memory search used to be swallowed by the knowledge handler."""
    services = _memory_services()
    update = _FakeUpdate("لغة برمجة")
    context = _FakeContext(services, user_data={"memory_mode": "search"})

    await H.text_router(update, context)

    services.memory.search.assert_called_once_with(42, "لغة برمجة")
    update.message.reply_text.assert_awaited_once()
    assert context.user_data["memory_mode"] is None  # mode consumed


async def test_memory_forget_flow_receives_the_typed_text():
    """'forget' is a privacy control: it must reach the memory service."""
    services = _memory_services()
    update = _FakeUpdate("7")
    context = _FakeContext(services, user_data={"memory_mode": "forget"})

    await H.text_router(update, context)

    services.memory.forget.assert_called_once_with(42, 7)
    update.message.reply_text.assert_awaited_once()


async def test_memory_edit_flow_receives_the_typed_text():
    services = _memory_services()
    update = _FakeUpdate("3 أحب الشاي")
    context = _FakeContext(services, user_data={"memory_mode": "edit"})

    await H.text_router(update, context)

    services.memory.edit.assert_called_once_with(42, 3, "أحب الشاي")


async def test_knowledge_ask_mode_still_reaches_the_rag_flow(monkeypatch):
    # The AI guard is a process-wide budget; other tests may have spent it.
    # Give this test a fresh, generous one instead of depending on test order.
    wide = {name: RateLimitConfig(max_requests=10_000, window_seconds=60)
            for name in DEFAULT_LIMITS}
    monkeypatch.setattr(get_rate_limit_middleware(), "limiter", RateLimiter(limits=wide))

    services = _memory_services()
    update = _FakeUpdate("ما هو التشفير؟")
    context = _FakeContext(services, user_data={"knowledge_mode": "ask"})

    await H.text_router(update, context)

    services.knowledge.answer.assert_awaited_once_with(42, "ما هو التشفير؟")
    services.memory.search.assert_not_called()
    assert context.user_data["knowledge_mode"] is None


async def test_unrelated_text_is_not_answered_and_spends_no_ai_budget():
    services = _memory_services()
    update = _FakeUpdate("نص عابر")
    context = _FakeContext(services, user_data={})

    await H.text_router(update, context)

    services.knowledge.answer.assert_not_called()
    services.memory.search.assert_not_called()
    update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# CANCEL-1: /cancel really ends an active conversation
# ---------------------------------------------------------------------------


def test_cancel_handler_is_registered_after_every_conversation():
    app = M.build_application()
    group = app.handlers[0]
    cancel_index = next(
        index
        for index, handler in enumerate(group)
        if isinstance(handler, CommandHandler)
        and getattr(handler.callback, "__name__", "") == "cancel_command"
    )
    conversation_indexes = [
        index
        for index, handler in enumerate(group)
        if isinstance(handler, ConversationHandler)
    ]
    assert conversation_indexes, "expected conversation handlers to be registered"
    assert cancel_index > max(conversation_indexes), (
        "/cancel must be registered after the conversations, otherwise the "
        "global handler wins and the conversation state is never cleared"
    )


async def test_cancel_clears_an_active_conversation(monkeypatch):
    """Drive the real handler graph: /cancel must leave the conversation."""
    app = M.build_application()
    conversation = next(
        handler for handler in app.handlers[0] if isinstance(handler, ConversationHandler)
    )
    update = _real_update("/cancel", bot=_fake_bot(app))
    key = conversation._get_key(update)
    conversation._conversations[key] = 0  # pretend the flow is at its first state

    # No network: the fallback only answers through Message.reply_text.
    reply = AsyncMock(return_value=None)
    monkeypatch.setattr(Message, "reply_text", reply, raising=False)

    chosen = _first_matching(app, update)
    assert chosen is conversation, (
        "an active conversation must claim /cancel so its fallback_cancel runs; "
        f"got {type(chosen).__name__} instead"
    )

    context = app.context_types.context.from_update(update, app)
    await chosen.handle_update(update, app, chosen.check_update(update), context)

    assert key not in conversation._conversations
    reply.assert_awaited_once()
    assert reply.await_args.args[0] == "تم الإلغاء"


async def test_cancel_outside_a_conversation_answers_and_clears_nothing(monkeypatch):
    app = M.build_application()
    update = _real_update("/cancel", bot=_fake_bot(app))
    reply = AsyncMock(return_value=None)
    monkeypatch.setattr(Message, "reply_text", reply, raising=False)

    chosen = _first_matching(app, update)
    assert isinstance(chosen, CommandHandler)
    assert chosen.callback is M.cancel_command
    # every conversation must stay idle, or the next message would be eaten
    assert all(
        not handler._conversations
        for handler in app.handlers[0]
        if isinstance(handler, ConversationHandler)
    )


def test_text_filters_are_still_excluded_from_cancel_commands():
    """Sanity: the router only sees non-command text, as before."""
    app = M.build_application()
    text_handler = _global_text_handlers(app)[0]
    assert text_handler.filters is not None
    assert not text_handler.filters.check_update(_real_update("/cancel")), (
        "commands must be dispatched to CommandHandlers, not to text_router"
    )
    assert filters.TEXT.check_update(_real_update("hello"))


def test_router_handles_every_declared_text_mode():
    """The declared mode keys and the router's branches must stay in step."""
    import inspect

    source = inspect.getsource(H.text_router)
    assert set(H.TEXT_MODE_KEYS) == {"knowledge_mode", "memory_mode"}
    for key in H.TEXT_MODE_KEYS:
        assert key in source, f"text_router ignores the declared mode {key!r}"
    assert "memory_text(" in source and "knowledge_question(" in source
