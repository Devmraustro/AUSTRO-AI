"""Telegram-level privacy flows: /export, /delete confirmation, source deletion.

The point of these tests is the *user-visible contract*:

* `/delete` never destroys anything by itself - it only offers a confirmation;
* a stale token, a token from another context, or a replayed tap is refused;
* a failed erasure is reported as a failure, not as success;
* a verified erasure does not claim more than it verified;
* `/export` sends a real JSON document;
* the books list exposes a working per-source delete button.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-privacy-token")
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["DB_PATH"] = str(
    pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_privacy.db"
)

import pytest

from app.domain.context import UserContext
from app.domain.entities import NewPlan
from app.telegram import handlers as H
from app.telegram import main as M
from app.telegram import privacy_handlers as P

OWNER = 7301


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeDocumentMessage:
    def __init__(self):
        self.text = ""
        self.reply_text = AsyncMock(return_value=None)
        self.reply_document = AsyncMock(return_value=None)


class _FakeCallbackQuery:
    def __init__(self, data="x"):
        self.data = data
        self.message = _FakeDocumentMessage()
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.username = f"u{user_id}"
        self.first_name = "U"


class _FakeUpdate:
    def __init__(self, text="", data="x", callback=False, user_id=OWNER):
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = MagicMock(id=111)
        if callback:
            self.callback_query = _FakeCallbackQuery(data)
            self.message = None
        else:
            self.callback_query = None
            self.message = _FakeDocumentMessage()


class _FakeContext:
    def __init__(self, container, user_data=None):
        self.bot_data = {"container": container}
        self.user_data = dict(user_data or {})
        self.chat_data = {}
        self.application = MagicMock()
        self.application.create_task = MagicMock()


@pytest.fixture(autouse=True)
def wide_rate_limits(monkeypatch):
    from app.security.rate_limiter import DEFAULT_LIMITS

    monkeypatch.setattr(
        "app.security.rate_limiter.DEFAULT_LIMITS",
        {key: (10_000, 60) for key in DEFAULT_LIMITS},
    )
    from app.security import rate_limiter as rl
    rl.get_rate_limit_middleware.cache_clear() if hasattr(
        rl.get_rate_limit_middleware, "cache_clear"
    ) else None


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the shared database at a per-test file (the suite does the same)."""
    import database as database_module
    from app.database.connection import _CORE_INDEXES_DDL, _CORE_TABLES_DDL
    from app.database.migrations import apply_migrations

    path = str(tmp_path / "privacy_tg.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    for statement in _CORE_TABLES_DDL:
        cursor.execute(statement)
    for statement in _CORE_INDEXES_DDL:
        cursor.execute(statement)
    apply_migrations(conn)
    conn.commit()
    conn.close()

    database_module.db._close_connection()
    database_module.db.db_path = path
    database_module.db._local.connection = None
    monkeypatch.setenv("DB_PATH", path)
    yield path
    database_module.db._close_connection()


def _container():
    from app.core.container import build_container

    return build_container()


def _owner(container, user_id=OWNER):
    container.users.ensure_user(UserContext(user_id, "learner", "Learner", ""))


def _seed(container, user_id=OWNER):
    _owner(container, user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    container.plans.create(NewPlan(
        user_id=user_id, date=today, tasks=[{"task": "مهمة"}],
        priorities=[], review_time="20:00", break_time="10",
    ))
    container.db.log_activity(user_id, "test", "activity")


def _rendered(update) -> str:
    if update.callback_query is not None:
        return "\n".join(
            call.args[0] if call.args else ""
            for call in update.callback_query.edit_message_text.call_args_list
        )
    return "\n".join(
        call.args[0] if call.args else ""
        for call in update.message.reply_text.call_args_list
    )


# ---------------------------------------------------------------------------
# /export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_sends_a_real_json_document():
    container = _container()
    _seed(container)

    update = _FakeUpdate()
    await M.export_command(update, _FakeContext(container))

    assert update.message.reply_document.await_count == 1
    kwargs = update.message.reply_document.await_args.kwargs
    payload = json.loads(kwargs["document"].decode("utf-8"))
    assert payload["user_id"] == OWNER
    assert payload["tables"]["daily_plans"], "the export lost the user's plan"
    assert kwargs["filename"].endswith(".json")


@pytest.mark.asyncio
async def test_export_failure_is_reported_not_hidden(monkeypatch):
    container = _container()
    _seed(container)
    monkeypatch.setattr(
        type(container.privacy), "export", lambda self, user_id: None,
    )
    update = _FakeUpdate()
    await M.export_command(update, _FakeContext(container))

    assert update.message.reply_document.await_count == 0
    assert "تعذر" in _rendered(update)


# ---------------------------------------------------------------------------
# /delete — the confirmation contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_alone_deletes_nothing_and_asks_for_confirmation():
    container = _container()
    _seed(container)

    update = _FakeUpdate()
    context = _FakeContext(container)
    await M.delete_account_command(update, context)

    # Nothing destroyed by the command itself.
    assert container.plans.for_date(OWNER, datetime.utcnow().strftime("%Y-%m-%d"))
    assert container.db.get_user(OWNER) is not None

    text = _rendered(update)
    assert "حذف الحساب" in text
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    confirm = [b for b in buttons if b.callback_data.startswith("privacy_delete_confirm:")]
    assert confirm, "no confirmation button was offered"
    assert context.user_data[P._PENDING_KEY]["token"] in confirm[0].callback_data


@pytest.mark.asyncio
async def test_confirmation_deletes_and_reports_verified_result():
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    token = context.user_data[P._PENDING_KEY]["token"]

    tap = _FakeUpdate(callback=True, data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(tap, context)

    assert container.db.get_user(OWNER) is None
    text = _rendered(tap)
    assert "✅" in text and "نهائيًا" in text
    # The pending request is consumed: a replay cannot delete again.
    assert P._PENDING_KEY not in context.user_data


@pytest.mark.asyncio
async def test_replayed_confirmation_is_refused():
    """A second tap of the same button must not re-run the erasure."""
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    token = context.user_data[P._PENDING_KEY]["token"]

    first = _FakeUpdate(callback=True, data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(first, context)
    # Re-create data, then replay the old button.
    _seed(container)

    replay = _FakeUpdate(callback=True, data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(replay, context)

    assert container.db.get_user(OWNER) is not None, (
        "a replayed token deleted an account it no longer belonged to"
    )
    assert "انتهت صلاحية" in _rendered(replay)


@pytest.mark.asyncio
async def test_expired_confirmation_is_refused():
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    pending = context.user_data[P._PENDING_KEY]
    pending["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)

    tap = _FakeUpdate(callback=True,
                      data=f"privacy_delete_confirm:{pending['token']}")
    await P.delete_confirm_callback(tap, context)

    assert container.db.get_user(OWNER) is not None
    assert "انتهت صلاحية" in _rendered(tap)


@pytest.mark.asyncio
async def test_confirmation_for_another_user_is_refused():
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    token = context.user_data[P._PENDING_KEY]["token"]

    # A different Telegram account replays the button.
    tap = _FakeUpdate(callback=True, user_id=OWNER + 1,
                      data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(tap, context)

    assert container.db.get_user(OWNER) is not None
    assert "لا يخصّك" in _rendered(tap) or "انتهت" in _rendered(tap)


@pytest.mark.asyncio
async def test_cancel_deletes_nothing():
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)

    cancel = _FakeUpdate(callback=True, data="privacy_delete_cancel")
    await P.delete_cancel_callback(cancel, context)

    assert container.db.get_user(OWNER) is not None
    assert P._PENDING_KEY not in context.user_data
    assert "لم يُحذف" in _rendered(cancel)


@pytest.mark.asyncio
async def test_failed_erasure_is_reported_as_a_failure(monkeypatch):
    """If the transaction rolls back, the user must be told nothing was deleted."""
    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    token = context.user_data[P._PENDING_KEY]["token"]

    monkeypatch.setattr(
        type(container.privacy), "delete_account", lambda self, user_id: None,
    )
    tap = _FakeUpdate(callback=True, data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(tap, context)

    text = _rendered(tap)
    assert "تعذر" in text and "لم يُحذف" in text
    assert "✅" not in text, "a failed erasure was reported as success"


@pytest.mark.asyncio
async def test_unverified_erasure_does_not_claim_success(monkeypatch):
    """A residual row must be reported as incomplete, never as clean."""
    from app.application.privacy import DeletionResult

    container = _container()
    _seed(container)
    context = _FakeContext(container)
    await M.delete_account_command(_FakeUpdate(), context)
    token = context.user_data[P._PENDING_KEY]["token"]

    monkeypatch.setattr(
        type(container.privacy), "delete_account",
        lambda self, user_id: DeletionResult(
            deleted={"goals": 1}, files_removed=0, files_failed=0,
            verified=False, residual={"goals": 1},
        ),
    )
    tap = _FakeUpdate(callback=True, data=f"privacy_delete_confirm:{token}")
    await P.delete_confirm_callback(tap, context)

    text = _rendered(tap)
    assert "⚠️" in text and "الدعم" in text
    assert "✅" not in text


# ---------------------------------------------------------------------------
# Knowledge source deletion is reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_books_list_offers_a_delete_button_per_source():
    container = _container()
    _owner(container)
    # Seed two sources directly: the ingestion pipeline is not under test here.
    for title in ("كتاب أ", "كتاب ب"):
        container.db._get_connection().execute(
            "INSERT INTO knowledge_sources (owner_user_id, source_type, title, status) "
            "VALUES (?, 'book', ?, 'completed')",
            (OWNER, title),
        )
    container.db._get_connection().commit()

    update = _FakeUpdate(callback=True, data="knowledge_books")
    await H.knowledge_books(update, _FakeContext(container))

    markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    deletes = [item for item in data if item.startswith("kdel:")]
    assert len(deletes) == 2, f"expected one delete button per source, got {data}"


@pytest.mark.asyncio
async def test_source_delete_callback_reports_the_real_outcome():
    container = _container()
    _owner(container)
    conn = container.db._get_connection()
    conn.execute(
        "INSERT INTO knowledge_sources (owner_user_id, source_type, title, status) "
        "VALUES (?, 'book', 'كتاب', 'completed')", (OWNER,),
    )
    conn.commit()
    source_id = conn.execute(
        "SELECT MAX(source_id) FROM knowledge_sources"
    ).fetchone()[0]

    tap = _FakeUpdate(callback=True, data=f"kdel:{source_id}")
    await P.delete_source_callback(tap, _FakeContext(container))

    assert "حُذف" in _rendered(tap)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM knowledge_sources WHERE source_id = ?", (source_id,),
    ).fetchone()[0]
    assert remaining == 0


@pytest.mark.asyncio
async def test_source_delete_callback_failure_is_reported(monkeypatch):
    container = _container()
    _owner(container)
    monkeypatch.setattr(
        type(container.knowledge), "delete_source", lambda self, u, s: False,
    )
    tap = _FakeUpdate(callback=True, data="kdel:999")
    await P.delete_source_callback(tap, _FakeContext(container))
    assert "تعذر" in _rendered(tap)


def test_privacy_handlers_are_registered_in_the_application():
    """The commands must be reachable, not merely defined."""
    from app.telegram.main import build_application

    app = build_application()
    commands = set()
    patterns = []
    for group in app.handlers.values():
        for handler in group:
            command = getattr(handler, "commands", None)
            if command:
                commands.update(command)
            pattern = getattr(handler, "pattern", None)
            if pattern is not None and hasattr(pattern, "pattern"):
                patterns.append(pattern.pattern)
    assert "export" in commands
    assert "delete" in commands
    assert any("kdel" in p for p in patterns), "source deletion is unreachable"
    assert any("privacy_delete_confirm" in p for p in patterns)
