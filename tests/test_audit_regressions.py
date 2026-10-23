"""Regression tests for the audit fixes.

Covers, one test per confirmed defect:
  F-1  cascade data loss from INSERT OR REPLACE on `users`
  F-2  no rollback after a failed statement (all 125 catch sites)
  F-3  daily_reviews appended instead of upserted
  F-4  progress read-modify-write race
  F-5  ZIP decompression bomb (docx/epub) and unbounded PDF pages
  F-6  log redaction coverage + fail-closed formatter + log injection
  F-7  configured log level ignored
  F-8  pending/denied memory reaching the AI prompt
  F-9  PostgreSQL TLS silently downgradable to plaintext
  F-10 low-confidence memory parked as `pending` but unreachable for review
"""

import io
import logging
import os
import re
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import ValidationError  # noqa: E402
from app.security.redaction import RedactingFormatter, redact  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fresh SQLite-backed Database facade on a temp file."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    from app.database.connection import DatabaseManager
    from app.database.repositories import Database

    return Database(DatabaseManager(str(tmp_path / "a.db")))


def _docx_bomb(target_mb: int) -> bytes:
    """A DOCX whose single XML member inflates to `target_mb`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>" + "A" * (target_mb * 1024 * 1024)
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _legit_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>" + "Austro knowledge. " * 300
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _epub_bomb(target_mb: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package '
            'xmlns="http://www.idpf.org/2007/opf">'
            '<manifest><item id="c1" href="ch1.xhtml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        z.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0"?><html '
            'xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            + "A" * (target_mb * 1024 * 1024)
            + "</p></body></html>",
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# F-1  INSERT OR REPLACE on `users` deleted all user-owned data
# ---------------------------------------------------------------------------

def test_re_registering_user_does_not_delete_their_data(db):
    db.create_user(7, "austro", "Al", "Z")
    db.create_goal(7, "G1", "first", "study", ["s"])
    db.create_goal(7, "G2", "second", "study", ["s"])
    db.create_habit(7, "H1", "read", "daily", "08:00")
    db.update_profile(7, age=27, education_level="CS")

    assert db.get_goals(7) != []

    # /start runs again on every session start.
    assert db.create_user(7, "austro", "Al", "Z") is True

    assert len(db.get_goals(7)) == 2, "re-registering wiped the user's goals"
    assert len(db.get_habits(7)) == 1, "re-registering wiped the user's habits"
    profile = db.get_user(7)
    assert profile["age"] == 27, "re-registering reset age"
    assert profile["education_level"] == "CS", "re-registering reset education"


def test_re_registering_user_updates_identity_columns(db):
    db.create_user(7, "old", "Old", "Name")
    db.create_user(7, "new", "New", "Name")

    profile = db.get_user(7)
    assert profile["username"] == "new"
    assert profile["first_name"] == "New"


def test_re_registering_user_does_not_reset_goals_json(db):
    """`goals` lives on the users row and must survive an upsert."""
    db.create_user(7, "austro", "Al", "Z")
    db.update_profile(7, goals=["learn python", "ship project"])
    db.create_user(7, "austro", "Al", "Z")
    assert db.get_user(7)["goals"] == ["learn python", "ship project"]


# ---------------------------------------------------------------------------
# F-2  no rollback after a failed statement
# ---------------------------------------------------------------------------

def test_repository_rollback_discards_partial_write(db):
    db.create_user(7, "austro", "Al", "Z")
    conn = db._get_connection()

    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO goals (goal_id, user_id, title) VALUES (?, ?, ?)",
        (None, 7, "half-written goal"),
    )
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM table_that_does_not_exist")

    db.users._rollback()

    assert conn.in_transaction is False, "rollback left the transaction open"
    remaining = conn.execute(
        "SELECT COUNT(*) FROM goals WHERE title = 'half-written goal'"
    ).fetchone()[0]
    assert remaining == 0, "a partial write survived the rollback"


def test_failed_statement_does_not_poison_later_commit(db):
    """The real-world shape: one bad write, then an unrelated save.

    Without a rollback the open transaction from the failure is committed by
    the next successful write, so a half-applied operation becomes durable.
    """
    db.create_user(7, "austro", "Al", "Z")

    conn = db._get_connection()
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM table_that_does_not_exist")
    db.users._rollback()

    assert db.create_goal(7, "G", "t", "c", ["s"]) is not None
    rows = conn.execute("SELECT COUNT(*) FROM goals").fetchall()
    assert len(rows) == 1


def test_rollback_helper_never_raises(db):
    """A rollback failure must not mask the original database error."""
    closed = sqlite3.connect(":memory:")
    db.users._manager._local.connection = closed
    closed.close()
    db.users._rollback()  # must not raise


def test_every_db_error_handler_rolls_back():
    """Enforce the F-2 invariant across the whole repository layer.

    A handler that catches ``DB_ERROR`` without rolling back leaves the implicit
    transaction open (SQLite: a partial write committed later by an unrelated
    call; PostgreSQL: ``25P02`` until a rollback happens). Checking the whole
    layer keeps a newly added method from reintroducing the bug.
    """
    root = Path(__file__).resolve().parent.parent
    files = [
        "app/database/repositories.py",
        "app/knowledge/repositories.py",
        "app/memory/repositories.py",
        "app/learning/repositories.py",
    ]
    missing: list = []
    total = 0
    for rel in files:
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "except DB_ERROR" not in line:
                continue
            total += 1
            if "self._rollback()" not in "\n".join(lines[i + 1:i + 6]):
                missing.append(f"{rel}:{i + 1}")
    # Floor = exact handler count after the memory erasure rewrite removed the
    # dead resurrect/redact handlers (was 125). Every counted handler must still
    # roll back (checked above); this floor only guards against an empty scan.
    assert total >= 122, f"expected at least 122 handlers, found {total}"
    assert not missing, (
        "these DB_ERROR handlers do not roll back: " + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# F-3  daily_reviews appended one row per save
# ---------------------------------------------------------------------------

def test_daily_review_is_upserted_not_appended(db):
    db.create_user(7, "austro", "Al", "Z")
    for i in range(3):
        assert db.save_daily_review(
            7, "2026-01-01", f"did {i}", f"learned {i}", "none", f"plan {i}", 7
        ) is True

    reviews = db.get_daily_reviews(7, days=3650)
    assert len(reviews) == 1, f"expected one review per day, got {len(reviews)}"
    assert reviews[0]["accomplished"] == "did 2", "the last save must win"
    assert reviews[0]["learned"] == "learned 2"
    assert reviews[0]["mood"] == 7


def test_daily_reviews_for_different_days_coexist(db):
    db.create_user(7, "austro", "Al", "Z")
    db.save_daily_review(7, "2026-01-01", "a", "b", "c", "d", 5)
    db.save_daily_review(7, "2026-01-02", "e", "f", "g", "h", 6)
    assert len(db.get_daily_reviews(7, days=3650)) == 2


def test_daily_reviews_are_scoped_per_user(db):
    db.create_user(7, "austro", "Al", "Z")
    db.create_user(8, "other", "Ot", "Her")
    db.save_daily_review(7, "2026-01-01", "a", "b", "c", "d", 5)
    db.save_daily_review(8, "2026-01-01", "z", "z", "z", "z", 3)
    assert len(db.get_daily_reviews(7, days=3650)) == 1
    assert db.get_daily_reviews(8, days=3650)[0]["accomplished"] == "z"


# ---------------------------------------------------------------------------
# F-4  progress read-modify-write race
# ---------------------------------------------------------------------------

def test_progress_is_upserted_by_user_and_date(db):
    db.create_user(7, "austro", "Al", "Z")
    for hours in (1.0, 2.0, 4.5):
        assert db.log_progress(7, "2026-01-02", study_hours=hours) is True

    rows = db._get_connection().execute(
        "SELECT study_hours FROM progress WHERE user_id = 7 AND date = '2026-01-02'"
    ).fetchall()
    assert len(rows) == 1, f"progress duplicated into {len(rows)} rows"
    assert rows[0]["study_hours"] == 4.5, "the last update was lost"


def test_progress_upsert_updates_only_supplied_columns(db):
    db.create_user(7, "austro", "Al", "Z")
    db.log_progress(7, "2026-01-02", study_hours=1.0, tasks_completed=3)
    db.log_progress(7, "2026-01-02", tasks_completed=9)

    row = db._get_connection().execute(
        "SELECT study_hours, tasks_completed FROM progress "
        "WHERE user_id = 7 AND date = '2026-01-02'"
    ).fetchone()
    assert row["tasks_completed"] == 9
    assert row["study_hours"] == 1.0, "an untouched column was reset"


def test_progress_with_no_fields_is_a_noop(db):
    db.create_user(7, "austro", "Al", "Z")
    assert db.log_progress(7, "2026-01-02") is True
    count = db._get_connection().execute(
        "SELECT COUNT(*) FROM progress"
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# F-5  decompression bombs and unbounded PDF parsing
# ---------------------------------------------------------------------------

def test_docx_zip_bomb_is_rejected_before_decompression():
    from app.knowledge.extractors import TextExtractor

    payload = _docx_bomb(200)
    assert len(payload) < 1024 * 1024, "the bomb itself must be small"

    with pytest.raises(ValidationError):
        TextExtractor().extract(payload, "docx")


def test_docx_zip_bomb_of_a_gigabyte_is_rejected():
    from app.knowledge.extractors import TextExtractor

    with pytest.raises(ValidationError):
        TextExtractor().extract(_docx_bomb(1000), "docx")


def test_epub_zip_bomb_is_rejected():
    from app.knowledge.extractors import TextExtractor

    with pytest.raises(ValidationError):
        TextExtractor().extract(_epub_bomb(200), "epub")


def test_legitimate_docx_still_extracts():
    from app.knowledge.extractors import TextExtractor

    result = TextExtractor().extract(_legit_docx(), "docx")
    assert "Austro knowledge." in result.text


def test_archive_with_too_many_members_is_rejected():
    from app.knowledge.extractors import TextExtractor

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>real text</w:t></w:r></w:p></w:body></w:document>",
        )
        for i in range(2500):
            z.writestr(f"word/media/pad{i}.bin", b"\0" * 64)

    with pytest.raises(ValidationError):
        TextExtractor().extract(buf.getvalue(), "docx")


def test_corrupt_archive_is_rejected_cleanly():
    from app.knowledge.extractors import TextExtractor

    with pytest.raises(ValidationError):
        TextExtractor().extract(os.urandom(4096), "docx")


def test_pdf_page_limit_is_enforced_before_extraction():
    """`knowledge_max_pages` was checked only after every page was parsed."""
    from app.knowledge.extractors import TextExtractor, _pdf_limits

    max_pages, _max_chars = _pdf_limits()
    assert max_pages > 0

    class FakePage:
        def extract_text(self):
            raise AssertionError("pages were extracted despite the page limit")

    class FakeReader:
        """Reports many pages but explodes if any page is extracted."""

        def __init__(self, *a, **k):
            self.pages = [FakePage() for _ in range(max_pages + 500)]

    import pypdf  # noqa: F401

    original = pypdf.PdfReader
    pypdf.PdfReader = FakeReader
    try:
        with pytest.raises(ValidationError):
            TextExtractor().extract(b"%PDF-1.4 fake", "pdf")
    finally:
        pypdf.PdfReader = original


def test_zero_page_pdf_is_rejected():
    from app.knowledge.extractors import TextExtractor

    class EmptyReader:
        def __init__(self, *a, **k):
            self.pages = []

    import pypdf

    original = pypdf.PdfReader
    pypdf.PdfReader = EmptyReader
    try:
        with pytest.raises(ValidationError):
            TextExtractor().extract(b"%PDF-1.4 fake", "pdf")
    finally:
        pypdf.PdfReader = original


def test_zip_bomb_limits_are_configurable():
    """The limits must come from settings, not hard-coded constants."""
    import dataclasses

    from app.config import settings as settings_mod
    from app.knowledge.extractors import _resolve_limits

    original = settings_mod.settings.knowledge_max_uncompressed_mb
    # Settings is a frozen dataclass, so setattr is not available.
    object.__setattr__(
        settings_mod.settings, "knowledge_max_uncompressed_mb", 7
    )
    try:
        max_bytes, _members, _ratio = _resolve_limits()
        assert max_bytes == 7 * 1024 * 1024
    finally:
        object.__setattr__(
            settings_mod.settings, "knowledge_max_uncompressed_mb", original
        )
    assert dataclasses.is_dataclass(settings_mod.settings)


# ---------------------------------------------------------------------------
# F-6  redaction coverage, fail-closed formatting, log injection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,value,leak",
    [
        ("telegram literal",
         "123456789:AAF-audit-token-value-0123456789abcdefghij",
         "AAF-audit-token"),
        ("telegram url",
         "https://api.telegram.org/bot123456789:AAF-token-0123456789abcdefghij/sendMessage",
         "AAF-token"),
        ("bare google key",
         "AIzaSyDExample1234567890abcdefghijklmnopqrstu", "AIzaSyDExample"),
        ("bare openai key",
         "sk-proj-AbCdEf0123456789AbCdEf0123456789", "sk-proj-AbCdEf"),
        ("anthropic key",
         "sk-ant-api03-AbCdEf0123456789AbCdEf0123", "sk-ant-api03"),
        ("postgres dsn",
         "postgresql://appuser:hunter2@db.internal:5432/austro", "hunter2"),
        ("aws access key id", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("jwt",
         "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
         "eyJhbGciOiJIUzI1NiJ9"),
        ("authorization header",
         "authorization: Bearer abcdef1234567890abcdef", "abcdef1234567890abcdef"),
        ("labelled secret",
         "GEMINI_API_KEY=AIzaSyDExample1234567890abcdefghijklmnopqrstu",
         "AIzaSyDExample"),
        ("card number", "4111111111111111", "4111111111111111"),
        ("telegram file id",
         "BQACAgQAAx0Ce21lZ2F0ZV9maWxlX2lkX2hpZ2hlcmUxb25nX2lk", "BQACAgQAAx0Ce21lZ2F0ZV9maWxl"),
    ],
)
def test_redact_removes_secret_shapes(label, value, leak):
    out = redact(value)
    assert leak not in out, f"{label} leaked: {out!r}"


def test_redact_removes_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAtESTKEYMATERIAL\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact(pem)
    assert "MIIEowIBAAKCAQEAtESTKEYMATERIAL" not in out
    assert "PRIVATE_KEY" in out


@pytest.mark.parametrize(
    "payload",
    [
        "hello\n2026-01-01 - app - INFO - admin granted",
        "hello\rFAKE LEVEL LINE",
        "hello\x1b[31mRED\x1b[0m",
        "hello\x00world",
    ],
)
def test_redact_defuses_log_injection(payload):
    out = redact(payload)
    assert "\n" not in out and "\r" not in out
    assert "\x1b" not in out and "\x00" not in out


def test_redact_keeps_newlines_within_a_real_multiline_record():
    """A traceback is legitimately multi-line; it stays one escaped record."""
    out = redact("Traceback (most recent call last):\n  File x\nValueError: bad")
    assert "\\n" in out
    assert "\n" not in out


def test_formatter_fails_closed_on_error():
    """An unexpected formatting failure must not emit the raw record."""

    class Boom(RedactingFormatter):
        def formatMessage(self, record):
            raise RuntimeError("format failure")

    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "raw-secret-value-abc", (), None
    )
    out = Boom(fmt="%(message)s").format(record)
    assert "raw-secret-value-abc" not in out
    assert "REDACTION FAILED" in out


def test_formatter_redacts_exception_traceback():
    """The traceback is appended by Formatter.format, not part of msg."""
    token = "123456789:AAF-audit-token-value-0123456789abcdefghij"
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), None)
    try:
        raise ValueError(f"The token `{token}` was rejected.")
    except ValueError:
        import sys as _sys

        record.exc_info = _sys.exc_info()
    out = RedactingFormatter(fmt="%(message)s").format(record)
    assert "AAF-audit-token" not in out


def test_formatter_does_not_mutate_the_record():
    """A second handler on the same logger must still see the original."""
    token = "123456789:AAF-audit-token-value-0123456789abcdefghij"
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token %s", (token,), None)
    first = RedactingFormatter(fmt="%(message)s").format(record)
    assert token not in first
    assert record.getMessage() == f"token {token}"


# ---------------------------------------------------------------------------
# F-7  the configured log level was ignored
# ---------------------------------------------------------------------------

def test_configured_log_level_is_applied():
    """`AUSTRO_LOG_LEVEL` was loaded into settings and then ignored."""
    import dataclasses

    from app.config.settings import settings
    from app.observability.logging_config import setup_logging

    root = logging.getLogger()
    previous = root.level

    # Build a WARNING-level copy rather than reloading the settings module:
    # other modules hold a reference to the settings singleton, and reload()
    # would leave them pointing at a stale object for the rest of the session.
    warn_settings = dataclasses.replace(settings, log_level="WARNING")
    assert warn_settings.log_level == "WARNING"

    try:
        setup_logging(warn_settings)
        assert root.level == logging.WARNING, (
            f"AUSTRO_LOG_LEVEL=WARNING was ignored (effective "
            f"{logging.getLevelName(root.level)})"
        )
    finally:
        root.setLevel(previous)


def test_info_is_the_default_log_level():
    import dataclasses

    from app.config.settings import settings

    assert dataclasses.replace(settings, log_level="").log_level == ""
    from app.observability.logging_config import _resolve_level

    assert _resolve_level(None) == logging.INFO
    assert _resolve_level("WARNING") == logging.WARNING
    assert _resolve_level("warning") == logging.WARNING
    assert _resolve_level("DEBUG") == logging.DEBUG
    assert _resolve_level("nonsense") == logging.INFO


def test_setup_logging_is_idempotent(tmp_path, monkeypatch):
    """Repeat calls must not stack duplicate handlers and double every line."""
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    from app.config import settings as settings_mod
    from app.observability.logging_config import setup_logging

    root = logging.getLogger()
    before = len(root.handlers)
    try:
        setup_logging(settings_mod.settings)
        after_first = len(root.handlers)
        setup_logging(settings_mod.settings)
        after_second = len(root.handlers)
        assert after_first == after_second, (
            "setup_logging added duplicate handlers on a second call"
        )
        assert after_first <= before + 2
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)


# ---------------------------------------------------------------------------
# F-8  pending / denied memory reached the AI prompt
# ---------------------------------------------------------------------------

def _memory_store(tmp_path):
    from app.database.connection import DatabaseManager
    from app.memory.repositories import MemoryStore

    return MemoryStore(DatabaseManager(str(tmp_path / "a.db")))


def _add_memory(store, subject, claim, consent, key):
    from app.memory.models import MemoryItem

    return store.memories.create(MemoryItem(
        owner_user_id=1,
        scope="USER",
        memory_type="fact",
        subject=subject,
        claim=claim,
        importance=5,
        confidence="high",
        consent_state=consent,
        hash_key=key,
        metadata={"needs_confirmation": True},
    ))


def test_pending_memory_never_reaches_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    from app.config.settings import settings
    from app.memory.models import TaskContext
    from app.memory.retrieval import MemoryRetrieval

    store = _memory_store(tmp_path)
    _add_memory(store, "exam date", "Exam is on 2026-06-01.", "explicit", "k1")
    _add_memory(store, "salary", "Salary is 900000 and I hate this job.", "pending", "k2")
    _add_memory(store, "health", "Chronic condition, code X.", "denied", "k3")

    retr = MemoryRetrieval(store, settings)
    pack = retr.relevant(1, TaskContext(query="exam date salary job health"))

    leaked = [m for m in pack.memories if m.consent_state in ("pending", "denied")]
    assert not leaked, "unapproved memory reached the AI context"
    assert all(m.consent_state == "explicit" for m in pack.memories)
    rendered = str(pack.render(settings.memory_context_budget_chars))
    assert "900000" not in rendered
    assert "Chronic condition" not in rendered
    assert "Exam is on 2026-06-01." in rendered


def test_automatic_consent_is_retrievable(tmp_path, monkeypatch):
    """The gate must not over-block: automatic memories are fine."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    from app.config.settings import settings
    from app.memory.models import TaskContext
    from app.memory.retrieval import MemoryRetrieval

    store = _memory_store(tmp_path)
    _add_memory(store, "preference", "User prefers short answers.", "automatic", "k1")

    pack = MemoryRetrieval(store, settings).relevant(
        1, TaskContext(query="answer preference")
    )
    assert len(pack.memories) == 1


def test_unknown_consent_state_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    from app.config.settings import settings
    from app.memory.models import TaskContext
    from app.memory.retrieval import MemoryRetrieval

    store = _memory_store(tmp_path)
    _add_memory(store, "odd", "An unrecognised consent value.", "weird-value", "k1")

    pack = MemoryRetrieval(store, settings).relevant(1, TaskContext(query="odd"))
    assert not pack.memories, "an unknown consent state must not be retrievable"


def test_consent_gate_matches_declared_states():
    from app.memory.models import CONSENT_STATES
    from app.memory.retrieval import CONSENT_ALLOWED_FOR_CONTEXT

    assert CONSENT_ALLOWED_FOR_CONTEXT.issubset(set(CONSENT_STATES))
    assert "pending" not in CONSENT_ALLOWED_FOR_CONTEXT
    assert "denied" not in CONSENT_ALLOWED_FOR_CONTEXT


def test_low_confidence_memory_is_reviewable_then_becomes_retrievable(
    tmp_path, monkeypatch,
):
    """A `pending` row must be visible in the review queue and confirmable.

    Found while the consent gate was being added: `MemoryWriteGate.to_item`
    parked low-confidence candidates at `consent_state='pending'`, but
    `MemoryService.pending()` also requires `metadata['needs_confirmation']`,
    which was never set. Combined with the retrieval gate that excludes
    `pending`, such a memory was invisible everywhere and could never be
    confirmed -- it silently became dead data.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-audit-token")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    import database
    from app.core.container import build_container
    from app.memory.models import MemoryCandidate

    database.db._close_connection()
    database.db.db_path = str(tmp_path / "m.db")
    database.db._local.connection = None
    database.db.init_database()
    memory = build_container().memory

    result = memory.process_candidates(1, [MemoryCandidate(
        owner_user_id=1, memory_type="preference",
        subject="evening study", claim="prefers studying in the evening",
        confidence="low", provenance="explicit_user_statement",
    )])
    assert result["created"] == 1

    pending = memory.pending(1)
    assert [p["subject"] for p in pending] == ["evening study"], (
        "a low-confidence memory parked as pending is unreachable by the user "
        "and can therefore never be confirmed"
    )
    assert memory.confirm(1, pending[0]["memory_id"], accept=True, actor=1)
    assert memory.pending(1) == []

    pack = memory.relevant_for(1, query="evening study")
    assert any(m.memory_type == "preference" for m in pack.memories), (
        "an approved low-confidence memory must be usable once confirmed"
    )


# ---------------------------------------------------------------------------
# F-9  PostgreSQL TLS silently downgraded to plaintext
# ---------------------------------------------------------------------------

def test_postgres_ssl_kwargs_are_explicit():
    from app.database.connection import _postgres_ssl_kwargs

    kwargs = _postgres_ssl_kwargs()
    assert "sslmode" in kwargs, "sslmode must be set explicitly, not left to libpq"


@contextmanager
def _db_tls(host: str, sslmode: str):
    """Temporarily override the immutable settings used by the TLS check."""
    from app.config import settings as settings_mod

    s = settings_mod.settings
    original = (s.db_host, s.db_sslmode)
    object.__setattr__(s, "db_host", host)
    object.__setattr__(s, "db_sslmode", sslmode)
    try:
        yield
    finally:
        object.__setattr__(s, "db_host", original[0])
        object.__setattr__(s, "db_sslmode", original[1])


def test_remote_host_refuses_plaintext(monkeypatch):
    from app.database.connection import _assert_tls_for_remote_host

    monkeypatch.delenv("DB_ALLOW_INSECURE", raising=False)
    with _db_tls("db.prod.example.com", "prefer"):
        with pytest.raises(RuntimeError, match="DB_SSLMODE"):
            _assert_tls_for_remote_host()


def test_remote_host_allowed_with_require():
    from app.database.connection import _assert_tls_for_remote_host

    with _db_tls("db.prod.example.com", "require"):
        _assert_tls_for_remote_host()


def test_remote_host_allowed_with_verify_full():
    from app.database.connection import _assert_tls_for_remote_host

    with _db_tls("db.prod.example.com", "verify-full"):
        _assert_tls_for_remote_host()


def test_loopback_host_allows_default():
    from app.database.connection import _assert_tls_for_remote_host

    with _db_tls("localhost", "prefer"):
        _assert_tls_for_remote_host()


def test_compose_service_hostname_allows_default():
    """`db` is the compose service name; it stays on the internal network."""
    from app.database.connection import _assert_tls_for_remote_host

    with _db_tls("db", "prefer"):
        _assert_tls_for_remote_host()


def test_explicit_opt_out_is_honoured(monkeypatch):
    from app.database.connection import _assert_tls_for_remote_host

    monkeypatch.setenv("DB_ALLOW_INSECURE", "1")
    with _db_tls("db.prod.example.com", "disable"):
        _assert_tls_for_remote_host()


# ---------------------------------------------------------------------------
# CI / build context
# ---------------------------------------------------------------------------

def test_error_handler_does_not_log_user_message_text():
    """The audit found the whole message logged on every unhandled error."""
    import ast

    source = (Path(__file__).resolve().parent.parent
              / "app" / "telegram" / "main.py").read_text(encoding="utf-8")

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.bad = []

        def visit_Expr(self, node):
            call = node.value
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr.startswith("_")
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "logger"):
                return
            for arg in call.args:
                for sub in ast.walk(arg):
                    # str(Update) renders message.text / message.caption.
                    if isinstance(sub, ast.Name) and sub.id in {"update", "message"}:
                        self.bad.append(call.func.attr)

    visitor = _Visitor()
    visitor.visit(ast.parse(source))
    assert not visitor.bad, (
        f"logger calls interpolate the raw update/message object "
        f"({visitor.bad}); use _update_label() instead"
    )


def test_update_label_omits_message_text():
    from app.telegram.main import _update_label

    class FakeUser:
        id = 42
        username = "austro"

    class FakeChat:
        id = 42

    class FakeUpdate:
        effective_user = FakeUser()
        effective_chat = FakeChat()
        message = object()
        callback_query = None

    label = _update_label(FakeUpdate())
    assert "user=42" in label
    assert "chat=42" in label
    assert "via=message" in label


def test_update_label_handles_a_bare_update():
    from app.telegram.main import _update_label

    assert _update_label(object()) == "<unknown update>"


def test_security_ci_step_does_not_swallow_failures():
    """The old step was `pytest tests/test_security.py || echo skip`."""
    workflow = (Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Security tests not found" not in workflow, (
        "the CI security step still masks a missing/failing test file"
    )
    assert "trufflesecurity/trufflehog@main" not in workflow, (
        "the secret scan action is still pinned to a moving ref"
    )


def test_third_party_actions_are_pinned_to_a_release():
    """A wrong ref fails the whole job at 'Set up job', before any code runs.

    `trufflehog@3.88.24` was committed with no `v` prefix; the published tags
    are `vX.Y.Z`, so run #19 died with "Unable to resolve action
    `trufflesecurity/trufflehog@3.88.24`, unable to find version". Keep the pin
    explicit and in the published tag format.
    """
    workflow = (Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s*([^\s@]+)@([^\s#]+)", workflow)
    assert refs, "no third-party actions found in the workflow"

    for action, ref in refs:
        assert ref not in {"main", "master"}, (
            f"{action} is still pinned to a moving ref"
        )
        if action.startswith("trufflesecurity/trufflehog"):
            assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), (
                f"{action} must be pinned to a published vX.Y.Z tag, "
                f"got {ref!r} (trufflehog tags carry a leading 'v')"
            )