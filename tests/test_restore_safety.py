"""Restore safety: the drill must refuse unsafe targets and never touch the
database or the archive's destination before the archive is authenticated.

Offline by design: psycopg2 and psql are replaced by recording fakes, so these
tests prove the ORDER of operations (no destructive SQL before verification)
without a PostgreSQL server.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import pg_backup, pg_restore_drill
from scripts.pg_backup import build_manifest, compress_archive
from scripts.restore_safety import (
    CONFIRM_PREFIX,
    IntegrityError,
    RestoreRefused,
    required_confirmation,
    resolve_admin_credentials,
    validate_restore_request,
    validate_restore_target,
    verify_backup_before_restore,
)

APP_DB = "austro_ai"
TARGET = "austro_ai_restore_drill"
ADMIN = "ops_admin"
ADMIN_PW = "admin-pw-not-real"
APP_PW = "app-pw-not-real"
SQL = "CREATE TABLE users (id int);\nINSERT INTO users VALUES (1),(2),(3);\n"
COUNTS = {"users": 3}


def _good_env(**overrides):
    env = {
        "DB_NAME": APP_DB,
        "DB_USER": "austro_app",
        "DB_PASSWORD": APP_PW,
        "DB_HOST": "db.example.test",
        "DB_PORT": "5432",
        "RESTORE_TARGET_DB": TARGET,
        "RESTORE_CONFIRM_DESTRUCTIVE": required_confirmation(TARGET),
        "RESTORE_ADMIN_USER": ADMIN,
        "RESTORE_ADMIN_PASSWORD": ADMIN_PW,
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------- fixtures --- #
def _make_archive(tmp_path: Path, *, key: bytes = None, name="austro_ai_backup_20260110_020000"):
    raw = tmp_path / "dump.sql"
    raw.write_text(SQL, encoding="utf-8")
    archive = tmp_path / f"{name}.sql.gz"
    content_sha, archive_sha, archive_bytes, content_bytes = compress_archive(
        raw, archive, key=key)
    manifest = build_manifest(
        stamp="20260110_020000", moment=pg_backup.utcnow().replace(year=2026, month=1, day=10),
        archive_name=archive.name, content_sha256=content_sha, archive_sha256=archive_sha,
        archive_bytes=archive_bytes, content_bytes=content_bytes, tables=["users"],
        counts=dict(COUNTS), dump_version="pg_dump 16.6", daily_keep=7, monthly_keep=30,
        encryption_enabled=key is not None,
    )
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    raw.unlink()
    return archive, manifest_path


class _Recorder:
    """Fake psycopg2.connect that answers the drill's SQL and records every call."""

    def __init__(self, tables=("users",), counts=COUNTS):
        self.events = []
        self.tables = list(tables)
        self.counts = dict(counts)

    def connect(self, **kwargs):
        self.events.append(("connect", kwargs.get("dbname"), kwargs.get("user")))
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, rec):
        self.rec = rec
        self.autocommit = False

    def set_isolation_level(self, _):
        pass

    def cursor(self):
        return _FakeCursor(self.rec)

    def close(self):
        pass


class _FakeCursor:
    def __init__(self, rec):
        self.rec = rec
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql_obj, *args):
        text = _render(sql_obj)
        if text.startswith("DROP DATABASE"):
            self.rec.events.append(("drop", text))
            self._rows = []
        elif text.startswith("CREATE DATABASE"):
            self.rec.events.append(("create", text))
        elif text.startswith("GRANT"):
            self.rec.events.append(("grant", text))
        elif "information_schema" in text:
            self._rows = [(t,) for t in self.rec.tables]
        elif text.startswith("SELECT count(*)"):
            table = text.split('"')[1]
            self._rows = [(self.rec.counts.get(table, 0),)]
        elif text == "SELECT 1":
            self._rows = [(1,)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


def _render(obj) -> str:
    """Flatten psycopg2.sql objects to text without needing a live connection."""
    from psycopg2 import sql as pgsql

    if isinstance(obj, pgsql.Composed):
        return "".join(_render(part) for part in obj.seq)
    if isinstance(obj, pgsql.Identifier):
        return '"' + '"."'.join(obj.strings) + '"'
    if isinstance(obj, pgsql.SQL):
        return obj.string
    if isinstance(obj, pgsql.Literal):
        return repr(obj.wrapped)
    return str(obj)


def _psql_ok(events):
    def runner(dbname, stream):
        events.append(("psql", dbname))
        stream.read()  # consume the verified SQL like psql would
        return subprocess.CompletedProcess([], 0, b"", b"")
    return runner


def _psql_fail(events):
    def runner(dbname, stream):
        events.append(("psql", dbname))
        return subprocess.CompletedProcess([], 3, b"", b"ERROR: boom")
    return runner


# --------------------------------------------------- target + authorization --- #
@pytest.mark.parametrize("target", [None, "", "   "])
def test_missing_target_is_refused_no_default_to_app_db(target):
    env = _good_env(RESTORE_TARGET_DB=target)
    with pytest.raises(RestoreRefused, match="never default"):
        validate_restore_request(env, app_db=APP_DB)


def test_target_equal_to_application_database_is_refused():
    with pytest.raises(RestoreRefused, match="application database"):
        validate_restore_target("austro_restore", app_db="austro_restore")


@pytest.mark.parametrize("name", ["austro_ai", "production", "postgres", "template1"])
def test_non_disposable_names_are_refused(name):
    with pytest.raises(RestoreRefused):
        validate_restore_target(name, app_db=APP_DB)


@pytest.mark.parametrize("name", ["Bad-Name", "x", "drop table; restore", "1restore"])
def test_invalid_target_names_are_refused(name):
    with pytest.raises(RestoreRefused):
        validate_restore_target(name, app_db=APP_DB)


def test_source_database_is_refused_as_target():
    with pytest.raises(RestoreRefused, match="backup source"):
        validate_restore_target("src_restore", app_db=APP_DB, source_db="src_restore")


@pytest.mark.parametrize("name", [
    "production_restore", "prod_restore", "PROD_RESTORE", "Prod-Restore",
    "prod.restore", "restore_prod", "restore-production", "live_restore",
    "primary_restore", "master_restore", "prodrestore", "ProdRestore",
    "austro_ai_production_restore", "Production-Restore",
])
def test_production_like_restore_targets_are_refused(name):
    """Production markers are refused whatever the case or separator."""
    with pytest.raises(RestoreRefused, match="production or live"):
        validate_restore_target(name, app_db=APP_DB)


@pytest.mark.parametrize("name", ["Austro-AI", "AUSTRO_AI", "austro.ai", "austroai",
                                  "Austro-AI-DB", "austro"])
def test_live_database_names_are_refused_in_any_spelling(name):
    """Collapsing separators shows a live name even when it is spelled differently."""
    with pytest.raises(RestoreRefused):
        validate_restore_target(name, app_db=APP_DB)


def test_source_database_name_in_other_spelling_is_refused():
    with pytest.raises(RestoreRefused, match="live database name"):
        validate_restore_target("SRC-Backup", app_db=APP_DB, source_db="src_backup")


@pytest.mark.parametrize("name", ["austro_ai_restore_drill", "restore_drill_01",
                                  "austro_ai_restore_20260110"])
def test_disposable_restore_names_remain_valid(name):
    assert validate_restore_target(name, app_db=APP_DB) == name


def test_production_refusal_happens_before_database_or_archive_work(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(RESTORE_TARGET_DB="Prod-Restore",
                                     RESTORE_CONFIRM_DESTRUCTIVE=required_confirmation("Prod-Restore")),
        connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_REFUSED
    assert rec.events == [] and events == []


@pytest.mark.parametrize("supplied", [None, "", "yes", "true", CONFIRM_PREFIX,
                                      required_confirmation("other_restore")])
def test_destructive_confirmation_must_name_this_target(supplied):
    env = _good_env(RESTORE_CONFIRM_DESTRUCTIVE=supplied)
    with pytest.raises(RestoreRefused, match="not authorised"):
        validate_restore_request(env, app_db=APP_DB)


def test_admin_credentials_are_explicit_and_never_default_to_postgres():
    with pytest.raises(RestoreRefused, match="RESTORE_ADMIN_USER"):
        resolve_admin_credentials({"RESTORE_ADMIN_PASSWORD": "x"})
    with pytest.raises(RestoreRefused, match="RESTORE_ADMIN_PASSWORD"):
        resolve_admin_credentials({"RESTORE_ADMIN_USER": "ops"})


def test_valid_request_returns_target_and_admin():
    assert validate_restore_request(_good_env(), app_db=APP_DB) == (TARGET, ADMIN, ADMIN_PW)


# ------------------------------------------------ drill refuses before any I/O --- #
def test_refused_request_touches_neither_archive_nor_database(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(RESTORE_TARGET_DB=APP_DB),
        connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_REFUSED
    assert rec.events == [] and events == []


def test_missing_confirmation_refuses_without_database_work(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(RESTORE_CONFIRM_DESTRUCTIVE="yes"),
        connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_REFUSED
    assert rec.events == [] and events == []


# ---------------------------------------------- archive authentication (no DB) --- #
def test_wrong_key_fails_before_any_database_operation(tmp_path, monkeypatch):
    right, wrong = bytes(range(32)), bytes(range(1, 33))
    archive, manifest = _make_archive(tmp_path, key=right)
    key_file = tmp_path / "key-wrong.bin"
    key_file.write_bytes(wrong)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_FAILED
    assert rec.events == [], "wrong key must not create, drop or connect"
    assert events == [], "wrong key must not stream SQL into the target"


def test_missing_key_for_encrypted_archive_fails_before_database(tmp_path, monkeypatch):
    archive, manifest = _make_archive(tmp_path, key=bytes(range(32)))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_FAILED
    assert rec.events == [] and events == []


def test_tampered_encrypted_archive_fails_before_database(tmp_path, monkeypatch):
    key = bytes(range(32))
    archive, manifest = _make_archive(tmp_path, key=key)
    key_file = tmp_path / "key.bin"
    key_file.write_bytes(key)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF  # flip one ciphertext byte
    archive.write_bytes(bytes(data))
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_FAILED
    assert rec.events == [] and events == []


def test_tampered_plain_archive_fails_before_database(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    manifest_doc = json.loads(manifest.read_text())
    # Archive replaced by a valid gzip of DIFFERENT SQL: archive sha no longer matches.
    archive.write_bytes(gzip.compress(b"DROP DATABASE austro_ai;\n"))
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_FAILED
    assert rec.events == [] and events == []
    assert manifest_doc["checksum_sha256"]


def test_content_mismatch_with_matching_archive_sha_is_detected(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["checksum_sha256"] = hashlib.sha256(b"something else").hexdigest()
    with pytest.raises(IntegrityError, match="decompressed SQL"):
        verify_backup_before_restore(
            archive, doc,
            archive_is_encrypted=pg_restore_drill.archive_is_encrypted,
            file_sha256=pg_restore_drill.file_sha256,
            open_sql=pg_restore_drill.open_archive_sql)


def test_encryption_declared_mismatch_is_refused(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["encryption"] = {"enabled": True, "algo": "aes-256-gcm", "key_version": "v1"}
    with pytest.raises(IntegrityError, match="declares encryption"):
        verify_backup_before_restore(
            archive, doc,
            archive_is_encrypted=pg_restore_drill.archive_is_encrypted,
            file_sha256=pg_restore_drill.file_sha256,
            open_sql=pg_restore_drill.open_archive_sql)


# ---------------------------------------------------------- successful drill --- #
def test_verified_restore_runs_in_order_and_validates(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_OK
    kinds = [e[0] for e in rec.events]
    # destructive DDL happens only after the (already passed) verification, in order
    assert kinds.index("drop") < kinds.index("create") < kinds.index("grant")
    assert events == [("psql", TARGET)]
    # the admin role does the destructive work; the application role validates
    assert ("connect", "postgres", ADMIN) in rec.events
    assert ("connect", TARGET, "austro_app") in rec.events
    # the application database is never dropped or created; only the target is
    ddl = [e[1] for e in rec.events if e[0] in ("drop", "create")]
    assert ddl and all(TARGET in text for text in ddl)
    assert all(f'"{APP_DB}"' not in text for text in ddl)


def test_row_count_mismatch_fails_the_drill(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder(counts={"users": 99})
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_ok(events))
    assert rc == pg_restore_drill.EXIT_FAILED


def test_psql_failure_is_reported_as_failure(tmp_path):
    archive, manifest = _make_archive(tmp_path)
    rec = _Recorder()
    events = []
    rc = pg_restore_drill.run_restore_drill(
        archive, manifest, _good_env(), connect=rec.connect, psql_runner=_psql_fail(events))
    assert rc == pg_restore_drill.EXIT_FAILED
    assert ("grant" not in [e[0] for e in rec.events])


def test_restore_drill_source_has_no_default_target_or_superuser():
    source = (Path(pg_restore_drill.__file__)).read_text(encoding="utf-8")
    assert '"postgres" if superuser' not in source
    assert 'os.environ.get("DB_NAME", "austro_ai")' not in source
    assert "RESTORE_TARGET_DB" in source and "RESTORE_ADMIN_USER" in source
