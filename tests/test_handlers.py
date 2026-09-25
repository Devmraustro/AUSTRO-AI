from telegram.ext import ConversationHandler

from handlers import (
    get_registration_handlers,
    get_goal_handlers,
    get_plan_handlers,
    get_study_handlers,
    get_english_handlers,
    get_programming_handlers,
    get_accountability_handlers,
    get_habit_handlers,
    get_reminder_handlers,
    get_menu_handlers,
    REVIEW_ACCOMPLISHED,
)


def test_all_conversation_factories_build():
    factories = [
        get_registration_handlers,
        get_goal_handlers,
        get_plan_handlers,
        get_study_handlers,
        get_english_handlers,
        get_programming_handlers,
        get_accountability_handlers,
        get_habit_handlers,
        get_reminder_handlers,
    ]
    for factory in factories:
        assert isinstance(factory(), ConversationHandler), factory.__name__


def test_review_conversation_has_accomplished_state():
    handler = get_accountability_handlers()
    assert REVIEW_ACCOMPLISHED in handler.states


def test_no_duplicate_get_reminder_handlers():
    import handlers as h
    factory = getattr(h, "get_reminder_handlers")
    assert isinstance(factory(), ConversationHandler)


def test_menu_handlers_build():
    handlers = get_menu_handlers()
    assert isinstance(handlers, list)
    assert len(handlers) >= 20