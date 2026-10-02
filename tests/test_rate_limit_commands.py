"""
AUSTRO AI - Reachability + guard contracts for main.py command entry points.

`app/telegram/main.py` owns the slash commands and is registered separately
from the inline-menu graph in `handlers.py`, so it needs its own explicit map.
This module proves the map matches the code AND that the code is reachable from
the real `build_application()` handler graph.
"""

import ast
import inspect
import os
import pathlib
import tempfile

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-rate-limit-commands-token")
os.environ["GEMINI_API_KEY"] = ""
os.environ["DB_PATH"] = str(
    pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_rate_limit_commands.db"
)

import pytest

from app.telegram import handlers as H
from app.telegram import main as M

COMMAND, AI, UPLOAD, EXPENSIVE, NONE = (
    H.COMMAND,
    H.AI,
    H.UPLOAD,
    H.EXPENSIVE,
    H.NONE,
)


# Slash commands and their declared bucket. `None` = deliberately unguarded,
# with the reason given, because the user must always be able to escape.
MAIN_COMMAND_RATELIMITS = {
    "start": COMMAND,
    "help_command": COMMAND,
    "plan_command": COMMAND,
    "goals_command": COMMAND,
    "habits_command": COMMAND,
    "progress_command": COMMAND,
    "review_command": COMMAND,
    "dashboard_command": COMMAND,
    "settings_command": COMMAND,
    "knowledge_command": COMMAND,
    # /coach renders the coach menu AND runs an AI analysis on demand.
    "coach_command": f"{COMMAND}+{AI}",
    # /cancel must always work so a throttled user can leave a conversation.
    "cancel_command": NONE,
}

# Internal lifecycle callbacks, not user-facing entry points.
NOT_USER_ENTRY_POINTS = {"error_handler", "scheduler_post_init", "scheduler_post_stop"}


def _main_coroutines():
    return [
        name
        for name, obj in vars(M).items()
        if inspect.iscoroutinefunction(obj)
        and getattr(obj, "__module__", "") == M.__name__
    ]


def _guarded_names(func_name):
    """Names of guard helpers referenced in the body of `func_name`."""
    source = pathlib.Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            seg = ast.get_source_segment(source, node) or ""
            return {
                guard
                for guard in ("guard_command_action", "guard_ai_action", "guard_expensive_action")
                if guard in seg
            }
    raise AssertionError(f"{func_name} not found in {M.__file__}")


# ============================================================================
# 1. Map matches the code
# ============================================================================


def test_every_user_facing_command_is_classified():
    unclassified = set(_main_coroutines()) - set(MAIN_COMMAND_RATELIMITS) - NOT_USER_ENTRY_POINTS
    assert not unclassified, (
        "slash commands missing from MAIN_COMMAND_RATELIMITS "
        f"(declare a bucket or justify as unguarded): {sorted(unclassified)}"
    )


@pytest.mark.parametrize("name,declared", sorted(MAIN_COMMAND_RATELIMITS.items()))
def test_declared_bucket_matches_the_guard_in_the_code(name, declared):
    """The map must not drift from the code; a stale entry is a silent bypass."""
    guards = _guarded_names(name)
    declared = str(declared)
    expected = set()
    for token in (COMMAND, AI, UPLOAD, EXPENSIVE, NONE):
        # Match compound constants (AI == "ai_request+global_ai") before the
        # single-bucket tokens, so "ai_request" is not looked up on its own.
        if token and token in declared:
            expected |= {
                COMMAND: {"guard_command_action"},
                AI: {"guard_ai_action"},
                UPLOAD: {"guard_upload_action"},
                EXPENSIVE: {"guard_expensive_action"},
                NONE: set(),
            }[token]
    assert guards == expected, (
        f"{name}: map declares {declared!r} but body uses {sorted(guards) or 'no guard'}"
    )


@pytest.mark.parametrize("name", sorted(MAIN_COMMAND_RATELIMITS))
def test_every_command_guards_before_touching_services(name):
    """A guard must precede any service lookup, DB call, or state change."""
    source = pathlib.Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            seg = ast.get_source_segment(source, node) or ""
            lines = seg.splitlines()
            guard_at = next(
                (i for i, l in enumerate(lines) if "guard_" in l and "await" in l),
                None,
            )
            if guard_at is None:
                assert MAIN_COMMAND_RATELIMITS[name] == NONE, (
                    f"{name} has no guard but is not declared uncharged"
                )
                return
            for i, l in enumerate(lines[:guard_at]):
                for side_effect in (
                    "get_services(",
                    "services.",
                    "ensure_user(",
                    "create_task(",
                    "set_state(",
                ):
                    assert side_effect not in l, (
                        f"{name}: {side_effect!r} runs at line {i + 1}, BEFORE the "
                        f"rate-limit guard on line {guard_at + 1}"
                    )
            return
    raise AssertionError(f"{name} not found")


# ============================================================================
# 2. Reachability: the map describes the REAL handler graph
# ============================================================================


def _registered_callbacks(app, depth=0):
    """Walk the real PTB handler graph, including nested conversations."""
    seen = set()

    def walk(handler, level):
        if level > 8:
            return
        callback = getattr(handler, "callback", None)
        if callback is not None and getattr(callback, "__name__", None):
            seen.add(callback.__name__)
        sub = getattr(handler, "conversation_handler", None)
        if sub is not None:
            walk(sub, level + 1)
        for state_handlers in (getattr(handler, "states", None) or {}).values():
            for entry in state_handlers:
                walk(entry, level + 1)
        for group in ("entry_points", "fallbacks"):
            for entry in getattr(handler, group, None) or []:
                walk(entry, level + 1)

    for group in app.handlers.values():
        for handler in group:
            walk(handler, depth)
    return seen


def test_mapped_commands_are_actually_registered():
    """Every mapped command must really be reachable from build_application()."""
    app = M.build_application()
    registered = _registered_callbacks(app)
    unreachable = sorted(set(MAIN_COMMAND_RATELIMITS) - registered)
    assert not unreachable, f"mapped but never registered: {unreachable}"


def test_registered_commands_are_all_mapped():
    """No registered command may escape the map."""
    app = M.build_application()
    registered = _registered_callbacks(app)
    declared = (
        set(MAIN_COMMAND_RATELIMITS)
        | set(H.ENTRY_POINT_RATELIMITS)
        | set(H.UNGUARDED_CONVERSATION_STATES)
        | {"fallback_cancel", "_ingest_book"}
    )
    unmapped = sorted(n for n in registered if n not in declared)
    assert not unmapped, f"registered but unclassified: {unmapped}"


def test_cancel_is_always_registered():
    """/cancel must stay wired up; it is the escape hatch from any throttle."""
    app = M.build_application()
    assert "cancel_command" in _registered_callbacks(app)
