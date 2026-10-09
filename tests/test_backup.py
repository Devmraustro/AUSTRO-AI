"""Automated PostgreSQL backup: retention, integrity, locking and scheduling.

Fully deterministic: the database connection and the `pg_dump` subprocess are
injected fakes, so the suite performs no network I/O and never touches a real
PostgreSQL server. A real end-to-end backup/restore is only ever run manually
against an explicitly disposable database (see DISASTER_RECOVERY.md).
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import backup_scheduler, pg_backup
from scripts.pg_backup import (
    ARCHIVE_SUFFIX,
    BACKUP_PREFIX,
    MANIFEST_SUFFIX,
    BackupRecord,
    acquire_lock,
    build_manifest,
    compress_archive,
    list_backups,
    parse_stamp,
    prune_backups,
    run_backup,
    select_retention,
    verify_archive,
    write_pgpass,
)

PASSWORD = "sup3r-secret-pg-password"
TABLES = ("users", "goals")
COUNTS = {"users": 3, "goals": 7}
DUMP_SQL = "-- AUSTRO dump\nCREATE TABLE users (id int);\n"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self):
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        self._sql = sql

    def fetchall(self):
        if "information_schema" in self._sql:
            return [(t,) for t in TABLES]
        return []

    def fetchone(self):
        for table in TABLES:
            if f'"{table}"' in self._sql:
                return (COUNTS[table],)
        return (0,)


class _FakeConnection:
    def __init__(self):
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        self.closed = True


def _fake_connect(**kwargs):
    connection = _FakeConnection()
    connection.kwargs = kwargs
    return connection


class _DumpRunner:
    """Records argv/env and writes the requested -f output file."""

    def __init__(self, returncode: int = 0, stderr: str = "", content: str = DUMP_SQL):
        self.returncode = returncode
        self.stderr = stderr
        self.content = content
        self.calls: list = []
        self.pgpass_contents: list = []
        self.pgpass_modes: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        env = kwargs.get("env") or {}
        pgpass = env.get("PGPASSFILE")
        if pgpass and Path(pgpass).exists():
            self.pgpass_contents.append(Path(pgpass).read_text(encoding="utf-8"))
            self.pgpass_modes.append(oct(stat.S_IMODE(os.stat(pgpass).st_mode)))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "pg_dump (PostgreSQL) 16.6\n", "")
        if self.returncode != 0:
            return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)
        out_path = Path(argv[argv.index("-f") + 1])
        out_path.write_text(self.content, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.internal.example")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "austro_ai")
    monkeypatch.setenv("DB_USER", "austro")
    monkeypatch.setenv("DB_PASSWORD", PASSWORD)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("PG_BIN", raising=False)
    return monkeypatch


def _moment(day: int, hour: int = 2, minute: int = 0, month: int = 1, year: int = 2026) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)


def _write_backup(outdir: Path, moment: dt.datetime, content: str = DUMP_SQL) -> BackupRecord:
    """Create a realistic, complete archive+manifest pair on disk."""
    stamp = moment.strftime("%Y%m%d_%H%M%S")
    base = f"{BACKUP_PREFIX}{stamp}"
    raw = outdir / f".{stamp}.part"
    raw.write_text(content, encoding="utf-8")
    content_sha, archive_sha, archive_bytes, content_bytes = compress_archive(
        raw, outdir / f"{base}{ARCHIVE_SUFFIX}"
    )
    raw.unlink()
    (outdir / f"{base}{MANIFEST_SUFFIX}").write_text(
        json.dumps(build_manifest(
            stamp=stamp, moment=moment, archive_name=f"{base}{ARCHIVE_SUFFIX}",
            content_sha256=content_sha, archive_sha256=archive_sha,
            archive_bytes=archive_bytes, content_bytes=content_bytes,
            tables=list(TABLES), counts=dict(COUNTS), dump_version="pg_dump 16.6",
            daily_keep=7, monthly_keep=30,
        ), indent=2),
        encoding="utf-8",
    )
    return BackupRecord(stamp, parse_stamp(stamp), outdir / f"{base}{ARCHIVE_SUFFIX}",
                        outdir / f"{base}{MANIFEST_SUFFIX}")


# ==================== secure pg_dump authentication ==================== #


def test_pg_dump_password_never_appears_in_argv(env, tmp_path):
    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    argv = runner.calls[0]["argv"]
    joined = " ".join(argv)
    assert PASSWORD not in joined
    assert "--no-password" in argv
    assert "-W" not in argv and "--password" not in argv


def test_pg_dump_authenticates_through_pgpass_file_only(env, tmp_path):
    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    call = runner.calls[0]
    assert "PGPASSWORD" not in call["env"]
    assert call["env"]["PGPASSFILE"]
    assert runner.pgpass_contents == [
        f"db.internal.example:5432:austro_ai:austro:{PASSWORD}\n"
    ]
    if os.name != "nt":
        assert runner.pgpass_modes == ["0o600"]


def test_pgpass_escapes_separators(tmp_path):
    path = write_pgpass(tmp_path, "h", "5432", "db", "u", "pa:ss\\word")
    assert path.read_text(encoding="utf-8") == "h:5432:db:u:pa\\:ss\\\\word\n"


def test_logs_never_contain_the_password(env, tmp_path, capsys):
    runner = _DumpRunner(returncode=1, stderr="pg_dump: password authentication failed")
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert PASSWORD not in capsys.readouterr().out


# ==================== successful backup ==================== #


def test_successful_backup_publishes_archive_and_manifest(env, tmp_path):
    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    records = list_backups(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.complete
    assert record.stamp == "20260110_020000"
    assert record.archive.name == f"{BACKUP_PREFIX}20260110_020000{ARCHIVE_SUFFIX}"
    # no temp/partial files left behind, and the lock is released
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / ".backup.lock").exists()

    manifest = json.loads(record.manifest.read_text(encoding="utf-8"))
    assert manifest["tables"] == len(TABLES)
    assert manifest["row_counts"] == COUNTS
    assert manifest["engine"] == "postgresql"
    assert "16.6" in manifest["pg_dump_version"]
    assert manifest["archive_bytes"] == record.archive.stat().st_size
    # contract consumed by scripts/pg_restore_drill.py
    payload = gzip.decompress(record.archive.read_bytes())
    assert hashlib.sha256(payload).hexdigest() == manifest["checksum_sha256"]
    assert hashlib.sha256(record.archive.read_bytes()).hexdigest() == manifest["archive_sha256"]


def test_verify_archive_detects_corruption(tmp_path):
    record = _write_backup(tmp_path, _moment(10))
    manifest = json.loads(record.manifest.read_text(encoding="utf-8"))
    record.archive.write_bytes(b"corrupted")
    with pytest.raises(AssertionError):
        verify_archive(record.archive, manifest["checksum_sha256"],
                       manifest["archive_sha256"])


def test_verify_archive_detects_truncated_gzip(tmp_path):
    record = _write_backup(tmp_path, _moment(10), content="x" * 4096)
    manifest = json.loads(record.manifest.read_text(encoding="utf-8"))
    with record.archive.open("r+b") as handle:
        handle.truncate(record.archive.stat().st_size // 2)
    with pytest.raises(Exception):
        verify_archive(record.archive, manifest["checksum_sha256"],
                       manifest["archive_sha256"])


# ==================== failure behaviour ==================== #


def test_pg_dump_failure_keeps_known_good_backup_and_leaves_no_partial(env, tmp_path):
    kept = _write_backup(tmp_path, _moment(9))
    before = sorted(p.name for p in tmp_path.iterdir())
    runner = _DumpRunner(returncode=1, stderr="pg_dump: error: connection refused")
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert kept.archive.exists() and kept.manifest.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / ".backup.lock").exists()


def test_row_count_failure_aborts_before_pg_dump(env, tmp_path):
    def _broken_connect(**kwargs):
        raise RuntimeError("could not connect to server")

    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_broken_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert runner.calls == []
    assert list_backups(tmp_path) == []


def test_unverified_new_archive_is_removed_but_older_backup_survives(env, tmp_path,
                                                                     monkeypatch):
    kept = _write_backup(tmp_path, _moment(9))
    calls = {"n": 0}
    real_verify = pg_backup.verify_archive

    def _fail_on_published_verify(archive, content_sha, archive_sha):
        calls["n"] += 1
        if calls["n"] == 2:  # corruption detected after publish
            raise AssertionError("archive sha256 mismatch after publish")
        return real_verify(archive, content_sha, archive_sha)

    monkeypatch.setattr(pg_backup, "verify_archive", _fail_on_published_verify)
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump")) == 1
    assert kept.archive.exists() and kept.manifest.exists()
    assert list_backups(tmp_path) == [kept]
    assert not (tmp_path / ".backup.lock").exists()


# ==================== duplicate-run protection ==================== #


def test_lock_blocks_a_concurrent_backup(env, tmp_path):
    existing = _write_backup(tmp_path, _moment(9))
    assert acquire_lock(tmp_path, now=_moment(10)) is not None
    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    assert runner.calls == []           # nothing executed
    assert list_backups(tmp_path) == [existing]  # nothing changed


def test_stale_lock_is_reclaimed_after_a_killed_run(env, tmp_path):
    # a run that died 5 hours ago (docker kill, host reboot, OOM)
    lock = acquire_lock(tmp_path, now=_moment(10))
    assert lock is not None
    dead_at = (_moment(10) - dt.timedelta(hours=5)).timestamp()
    os.utime(lock, (dead_at, dead_at))
    runner = _DumpRunner()
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    assert runner.calls
    assert len(list_backups(tmp_path)) == 1
    assert not (tmp_path / ".backup.lock").exists()


# ==================== retention ==================== #


def test_retention_keeps_seven_daily_plus_monthly_points(tmp_path):
    for day in range(1, 29):  # 28 consecutive daily backups
        _write_backup(tmp_path, _moment(day, month=1))
    for day in range(1, 29):
        _write_backup(tmp_path, _moment(day, month=2))
    records = list_backups(tmp_path)
    keep, drop = select_retention(records, daily_keep=7, monthly_keep=30)
    # 7 daily + one January recovery point; February is already represented in
    # the daily window, so it adds no duplicate monthly point.
    assert len(keep) == 8
    assert len(drop) == len(records) - len(keep)
    # the monthly point of a month is its NEWEST backup
    january = [r.created_utc for r in keep if r.created_utc.month == 1]
    assert january == [_moment(28, month=1)]


def test_retention_monthly_point_is_the_newest_of_each_month(tmp_path):
    oldest_march = _write_backup(tmp_path, _moment(5, month=3))
    newest = _write_backup(tmp_path, _moment(20, month=3))
    for day in range(1, 8):  # 7 newer daily backups in April
        _write_backup(tmp_path, _moment(day, month=4))
    keep, drop = select_retention(list_backups(tmp_path), daily_keep=7, monthly_keep=30)
    assert newest in keep
    assert {r.stamp for r in drop} == {oldest_march.stamp}


def test_retention_caps_monthly_points_at_thirty(tmp_path):
    for month in range(1, 49):  # four years of months, two backups each
        year = 2024 + (month - 1) // 12
        real_month = (month - 1) % 12 + 1
        for day in (1, 20):
            _write_backup(tmp_path, _moment(day, month=real_month, year=year))
    records = list_backups(tmp_path)
    keep, drop = select_retention(records, daily_keep=7, monthly_keep=30)
    months = {_month(r.created_utc) for r in keep}
    assert len(keep) == 7 + 30           # daily window + capped monthly points
    # the 7 daily span four months (2027-09..2027-12); the 30 monthly points
    # then walk backwards from 2027-08, so the oldest recovery point is 2025-03.
    assert len(months) == 34
    assert min(months) == (2025, 3)
    assert len(keep) + len(drop) == len(records)


def test_retention_never_deletes_the_only_backup(tmp_path):
    only = _write_backup(tmp_path, _moment(10))
    keep, drop = select_retention([only], daily_keep=1, monthly_keep=0)
    assert keep == [only] and drop == []


def test_retention_never_deletes_unparseable_stamp(tmp_path):
    archive = tmp_path / f"{BACKUP_PREFIX}not-a-timestamp{ARCHIVE_SUFFIX}"
    archive.write_bytes(b"junk")
    record = BackupRecord("not-a-timestamp", None, archive, None)
    keep, drop = select_retention([record], daily_keep=1, monthly_keep=0)
    assert keep == [record] and drop == []


def test_prune_deletes_archive_and_manifest_together(tmp_path):
    records = [_write_backup(tmp_path, _moment(day)) for day in range(1, 11)]
    _, drop = select_retention(records, daily_keep=3, monthly_keep=0)
    prune_backups(tmp_path, drop)
    remaining = list_backups(tmp_path)
    assert len(remaining) == 3
    assert all(r.complete for r in remaining)  # never an orphaned half-state
    assert sorted(r.created_utc for r in remaining) == [
        _moment(8), _moment(9), _moment(10)
    ]


def _month(moment: dt.datetime):
    return (moment.year, moment.month)


def test_run_backup_prunes_only_after_a_verified_new_backup(env, tmp_path):
    for day in range(1, 11):
        _write_backup(tmp_path, _moment(day))
    before = {p.name for p in tmp_path.iterdir()}

    # failing run: retention must not touch anything
    assert run_backup(tmp_path, now=_moment(11), connect=_fake_connect,
                      dump_runner=_DumpRunner(returncode=1, stderr="boom"),
                      tool=Path("pg_dump")) == 1
    assert {p.name for p in tmp_path.iterdir()} == before

    # successful run: retention applies
    assert run_backup(tmp_path, now=_moment(11), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump"),
                      daily_keep=7, monthly_keep=0) == 0
    assert len(list_backups(tmp_path)) == 7
    assert all(r.complete for r in list_backups(tmp_path))


def test_incomplete_artifact_is_never_auto_deleted(env, tmp_path):
    good = _write_backup(tmp_path, _moment(9))
    orphan = tmp_path / f"{BACKUP_PREFIX}20260108_020000{ARCHIVE_SUFFIX}"
    orphan.write_bytes(b"no manifest for me")
    assert run_backup(tmp_path, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump"),
                      daily_keep=2, monthly_keep=0) == 0
    assert orphan.exists()          # outside retention, kept for manual review
    assert good.archive.exists()    # inside the daily window
    assert len(list_backups(tmp_path)) == 3


# ==================== UTC scheduling ==================== #


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_moment(10, hour=1, minute=30), _moment(10, hour=2, minute=0)),
        (_moment(10, hour=2, minute=0), _moment(11, hour=2, minute=0)),
        (_moment(10, hour=23, minute=59), _moment(11, hour=2, minute=0)),
        (_moment(31, month=1, hour=3), _moment(1, month=2, hour=2, minute=0)),
        (_moment(31, month=12, hour=5, year=2026), _moment(1, month=1, year=2027)),
    ],
)
def test_next_run_utc_is_always_in_the_future(now, expected):
    assert backup_scheduler.next_run_utc(now) == expected


def test_next_run_utc_normalises_non_utc_input():
    tz = dt.timezone(dt.timedelta(hours=3))
    now = dt.datetime(2026, 1, 10, 6, 0, tzinfo=tz)  # 03:00 UTC
    assert backup_scheduler.next_run_utc(now) == _moment(11, hour=2, minute=0)


def test_has_backup_for_date(tmp_path):
    _write_backup(tmp_path, _moment(10))
    assert backup_scheduler.has_backup_for_date(tmp_path, _moment(10).date()) is True
    assert backup_scheduler.has_backup_for_date(tmp_path, _moment(11).date()) is False
    assert backup_scheduler.has_backup_for_date(tmp_path / "missing",
                                                _moment(10).date()) is False


def test_missed_run_is_recovered_on_restart(tmp_path):
    # 03:00 UTC, nothing backed up today -> catch-up run is required
    assert backup_scheduler.should_run_now(_moment(10, hour=3), tmp_path) is True
    _write_backup(tmp_path, _moment(10))
    assert backup_scheduler.should_run_now(_moment(10, hour=3), tmp_path) is False
    # before the scheduled hour nothing runs, and an empty dir is fine
    assert backup_scheduler.should_run_now(_moment(10, hour=1), tmp_path) is False


def test_run_scheduled_backup_forwards_the_retention_settings(tmp_path, monkeypatch):
    seen = {}

    def _fake_run_backup(outdir, **kwargs):
        seen["outdir"] = outdir
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(backup_scheduler, "run_backup", _fake_run_backup)
    assert backup_scheduler.run_scheduled_backup(tmp_path, 7, 30) == 1
    assert seen == {"outdir": tmp_path, "daily_keep": 7, "monthly_keep": 30}


def test_failed_day_uses_a_bounded_retry_backoff():
    # a failed backup must not turn into a hot retry loop against the database
    assert backup_scheduler.DEFAULT_RETRY_MINUTES >= 30


def test_scheduler_never_schedules_restore_drills():
    source = Path(backup_scheduler.__file__).read_text(encoding="utf-8")
    assert "pg_restore_drill" not in source
    assert "DROP DATABASE" not in source

# ==================== encrypted backup archive ==================== #
_ENCRYPTED_SQL = "".join(
    f"-- AUSTRO dump\nINSERT INTO t VALUES ({i});\n" for i in range(500)
)


def _write_raw(tmp_path: Path, body: bytes = None) -> Path:
    raw = tmp_path / "dump.sql.part"
    raw.write_bytes(body if body is not None else _ENCRYPTED_SQL.encode("utf-8"))
    return raw


def _key_file(tmp_path: Path, key: bytes) -> Path:
    path = tmp_path / "backup.key"
    path.write_bytes(key)
    return path


def test_encrypted_archive_is_not_plaintext_and_round_trips(tmp_path, monkeypatch):
    """A keyed backup is a real ciphertext container that decrypts back."""
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    key = os.urandom(32)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"

    content_sha, archive_sha, archive_bytes, content_bytes = compress_archive(
        raw, archive, key=key
    )

    assert pg_backup.archive_is_encrypted(archive) is True
    # The SQL must not be readable in the archive at all.
    assert b"INSERT INTO t VALUES" not in archive.read_bytes()
    assert key not in archive.read_bytes()
    # archive_bytes is the real file size and content_bytes the raw SQL size.
    assert archive_bytes == archive.stat().st_size
    assert content_bytes == len(_ENCRYPTED_SQL.encode("utf-8"))
    # checksums describe the original SQL, matching the plaintext contract.
    assert content_sha == hashlib.sha256(_ENCRYPTED_SQL.encode("utf-8")).hexdigest()

    verify_archive(archive, content_sha, archive_sha, key=key)
    with pg_backup.open_archive_sql(archive, key=key) as handle:
        assert handle.read() == _ENCRYPTED_SQL.encode("utf-8")


def test_plain_archive_still_round_trips_when_no_key_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"plain{ARCHIVE_SUFFIX}"

    content_sha, archive_sha, _, _ = compress_archive(raw, archive)

    assert pg_backup.archive_is_encrypted(archive) is False
    verify_archive(archive, content_sha, archive_sha)
    with gzip.open(archive, "rb") as handle:
        assert handle.read() == _ENCRYPTED_SQL.encode("utf-8")


def test_encrypted_archive_rejects_wrong_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"
    compress_archive(raw, archive, key=os.urandom(32))

    with pytest.raises(ValueError):
        with pg_backup.open_archive_sql(archive, key=os.urandom(32)) as handle:
            handle.read()


@pytest.mark.parametrize("offset_from_end", [1, -1], ids=["last_byte", "one_before_end"])
def test_encrypted_archive_rejects_tampering(tmp_path, monkeypatch, offset_from_end):
    """Flipping any byte of the ciphertext or tag must fail authentication."""
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    key = os.urandom(32)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"
    compress_archive(raw, archive, key=key)

    blob = bytearray(archive.read_bytes())
    index = len(blob) + offset_from_end if offset_from_end < 0 else len(blob) - 1
    blob[index] ^= 0xFF
    archive.write_bytes(bytes(blob))

    with pytest.raises(ValueError):
        with pg_backup.open_archive_sql(archive, key=key) as handle:
            handle.read()


def test_encryption_required_without_key_fails_closed(tmp_path, monkeypatch):
    """BACKUP_ENCRYPTION_ENABLED must never silently produce a plaintext dump."""
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    raw = _write_raw(tmp_path)

    with pytest.raises(RuntimeError):
        compress_archive(raw, tmp_path / f"enc{ARCHIVE_SUFFIX}")


def test_encryption_required_with_wrong_size_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(_key_file(tmp_path, b"too-short")))
    raw = _write_raw(tmp_path)

    with pytest.raises(RuntimeError):
        compress_archive(raw, tmp_path / f"enc{ARCHIVE_SUFFIX}")


def test_encryption_required_with_missing_key_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(tmp_path / "absent.key"))
    raw = _write_raw(tmp_path)

    with pytest.raises(RuntimeError):
        compress_archive(raw, tmp_path / f"enc{ARCHIVE_SUFFIX}")


def test_manifest_never_leaks_key_or_ciphertext_material(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    key = os.urandom(32)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"
    content_sha, archive_sha, archive_bytes, content_bytes = compress_archive(
        raw, archive, key=key
    )

    manifest = build_manifest(
        stamp="20260101_020000", moment=_moment(1),
        archive_name=archive.name, content_sha256=content_sha,
        archive_sha256=archive_sha, archive_bytes=archive_bytes,
        content_bytes=content_bytes, tables=list(TABLES), counts=dict(COUNTS),
        dump_version="pg_dump 16.6", daily_keep=7, monthly_keep=30,
        encryption_enabled=True,
    )
    blob = json.dumps(manifest)

    assert manifest["encryption"] == {
        "enabled": True, "algo": "aes-256-gcm", "key_version": "v1",
    }
    assert base64.b64encode(key).decode() not in blob
    assert key.hex() not in blob
    for forbidden in ("nonce", "tag", "ciphertext", "key_material"):
        assert forbidden not in manifest["encryption"]


def test_key_is_loaded_only_from_the_configured_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    key = os.urandom(32)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(_key_file(tmp_path, key)))

    assert pg_backup._load_encryption_key() == key


def test_key_file_tolerates_single_trailing_newline(tmp_path, monkeypatch):
    """Secret stores often append a newline; a 32-byte key must still load."""
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    key = os.urandom(32)
    path = _key_file(tmp_path, key + b"\n")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(path))

    assert pg_backup._load_encryption_key() == key


def test_verify_archive_rejects_encrypted_archive_without_key(tmp_path, monkeypatch):
    """Verification must fail closed rather than skip an encrypted archive."""
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"
    content_sha, archive_sha, _, _ = compress_archive(raw, archive, key=os.urandom(32))

    with pytest.raises(RuntimeError):
        verify_archive(archive, content_sha, archive_sha)


def test_encrypted_archive_uses_a_versioned_container_format(tmp_path, monkeypatch):
    """The container is 0x01 || nonce(12) || ciphertext || tag(16)."""
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    raw = _write_raw(tmp_path)
    archive = tmp_path / f"enc{ARCHIVE_SUFFIX}"
    compress_archive(raw, archive, key=os.urandom(32))

    blob = archive.read_bytes()
    assert blob[0] == 0x01
    assert len(blob) > 1 + 12 + 16


def test_dead_encryption_constants_are_removed():
    """No default key material or unused algo constant may linger."""
    assert not hasattr(pg_backup, "BACKUP_ENCRYPTION_DEFAULT_KEY")
    assert not hasattr(pg_backup, "BACKUP_ENCRYPTION_ALGO")


def test_backup_script_defines_each_helper_once():
    """Duplicate definitions silently shadow each other; guard against them."""
    tree = ast.parse(Path(pg_backup.__file__).read_text(encoding="utf-8"))
    names = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate definitions in pg_backup.py: {duplicates}"
    # The two helpers the audit called out must exist exactly once each.
    assert names.count("compress_archive") == 1
    assert names.count("build_manifest") == 1


def test_restore_drill_supports_encrypted_archives():
    """The drill must decrypt rather than assume a plain gzip member.

    Read as source: importing the module would open a database connection at
    import time, which this offline suite must never do.
    """
    source = (Path(pg_backup.__file__).parent / "pg_restore_drill.py").read_text(
        encoding="utf-8"
    )
    assert "open_archive_sql" in source
    assert "gzip.open(str(backup)" not in source

# ==================== plaintext hygiene + fail-closed encryption ==================== #


def _workdir(tmp_path: Path, monkeypatch) -> Path:
    work = tmp_path / "scratch-work"
    monkeypatch.setenv("BACKUP_WORKDIR", str(work))
    return work


def test_plaintext_dump_never_lands_in_the_backup_directory(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    work = _workdir(tmp_path, monkeypatch)
    runner = _DumpRunner()
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    dump_target = Path(runner.calls[0]["argv"][runner.calls[0]["argv"].index("-f") + 1])
    assert outdir.resolve() not in dump_target.resolve().parents
    assert work.resolve() in dump_target.resolve().parents
    # nothing plaintext-like survives in the persistent directory
    assert not [p for p in outdir.iterdir() if p.suffix == ".part" or p.suffix == ".sql"]


def test_plaintext_work_dir_is_private_and_removed_after_success(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    work = _workdir(tmp_path, monkeypatch)
    runner = _DumpRunner()
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 0
    assert work.is_dir() and list(work.iterdir()) == []
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(work).st_mode) == 0o755  # parent created by us
    dump_dir = Path(runner.calls[0]["argv"][runner.calls[0]["argv"].index("-f") + 1]).parent
    assert not dump_dir.exists()


def test_plaintext_work_dir_is_removed_after_pg_dump_failure(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    work = _workdir(tmp_path, monkeypatch)
    runner = _DumpRunner(returncode=1, stderr="pg_dump: error")
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert list(work.iterdir()) == []


def test_plaintext_work_dir_is_removed_after_verification_failure(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    work = _workdir(tmp_path, monkeypatch)

    def _always_fail(archive, content_sha, archive_sha, key=None, **_):
        raise AssertionError("archive sha256 mismatch after publish")

    monkeypatch.setattr(pg_backup, "verify_archive", _always_fail)
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump")) == 1
    assert list(work.iterdir()) == []
    assert list_backups(outdir) == []


def test_stale_plaintext_from_killed_run_is_purged(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    outdir.mkdir()
    work = _workdir(tmp_path, monkeypatch)
    legacy = outdir / f".{BACKUP_PREFIX}20251201_020000.sql.part"
    legacy.write_text("PLAINTEXT SECRET DUMP", encoding="utf-8")
    old_work = work / f"{pg_backup.WORK_PREFIX}abandoned"
    old_work.mkdir(parents=True)
    (old_work / "dump.sql").write_text("PLAINTEXT SECRET DUMP", encoding="utf-8")
    ancient = (_moment(10) - dt.timedelta(hours=5)).timestamp()
    os.utime(old_work, (ancient, ancient))
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump")) == 0
    assert not legacy.exists()
    assert not old_work.exists()
    assert list(work.iterdir()) == []


def test_fresh_foreign_work_dir_is_not_purged(env, tmp_path, monkeypatch):
    outdir = tmp_path / "backups"
    outdir.mkdir()
    work = _workdir(tmp_path, monkeypatch)
    live = work / f"{pg_backup.WORK_PREFIX}concurrent"
    live.mkdir(parents=True)
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump")) == 0
    assert live.is_dir()


def test_encryption_required_without_key_fails_before_any_database_contact(
        env, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY_FILE", raising=False)
    _workdir(tmp_path, monkeypatch)
    calls = {"connect": 0}

    def _counting_connect(**kwargs):
        calls["connect"] += 1
        return _fake_connect(**kwargs)

    runner = _DumpRunner()
    assert run_backup(tmp_path / "b", now=_moment(10), connect=_counting_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert calls["connect"] == 0 and runner.calls == []
    assert not (tmp_path / "scratch-work").exists() or not list(
        (tmp_path / "scratch-work").iterdir())


def test_encryption_required_with_invalid_key_fails_before_dump(env, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    key_file = tmp_path / "short.key"
    key_file.write_bytes(b"too-short")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    _workdir(tmp_path, monkeypatch)
    runner = _DumpRunner()
    assert run_backup(tmp_path / "b", now=_moment(10), connect=_fake_connect,
                      dump_runner=runner, tool=Path("pg_dump")) == 1
    assert runner.calls == []
    assert list_backups(tmp_path / "b") == []


def test_encrypted_run_publishes_no_plaintext_and_declares_encryption(env, tmp_path, monkeypatch):
    key = bytes(range(32))
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(key)
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    _workdir(tmp_path, monkeypatch)
    outdir = tmp_path / "backups"
    assert run_backup(outdir, now=_moment(10), connect=_fake_connect,
                      dump_runner=_DumpRunner(), tool=Path("pg_dump")) == 0
    record = list_backups(outdir)[0]
    blob = record.archive.read_bytes()
    assert blob[:1] == b"\x01"                      # encrypted container version byte
    assert b"AUSTRO dump" not in blob and b"CREATE TABLE" not in blob
    manifest = json.loads(record.manifest.read_text(encoding="utf-8"))
    assert manifest["encryption"]["enabled"] is True
    assert key.hex() not in record.manifest.read_text(encoding="utf-8")


def test_key_with_leading_and_trailing_whitespace_bytes_is_not_corrupted(tmp_path, monkeypatch):
    """A random key may start/end with \\r, \\t, space or \\n. Only the single
    trailing line terminator may be removed; stripping key bytes is corruption."""
    key = b"\r" + bytes(range(1, 31)) + b"\t"  # 32 bytes, both ends whitespace
    assert len(key) == 32
    path = tmp_path / "ws.key"
    path.write_bytes(key + b"\n")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(path))
    assert pg_backup._load_encryption_key() == key
    path.write_bytes(key)  # and the bare key is accepted as-is
    assert pg_backup._load_encryption_key() == key


def test_scheduler_refuses_to_start_without_encryption(monkeypatch, tmp_path):
    """The scheduled worker is encrypted-only: with the flag off it must exit 2
    immediately, before creating the output directory or entering the loop."""
    monkeypatch.delenv("BACKUP_ENCRYPTION_ENABLED", raising=False)
    outdir = tmp_path / "must-not-be-created"
    monkeypatch.setenv("BACKUP_OUTDIR", str(outdir))
    assert backup_scheduler.main() == 2
    assert not outdir.exists()


def test_scheduler_flag_must_be_truthy_to_pass_the_guard(monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "false")
    assert pg_backup.encryption_required() is False
    monkeypatch.setenv("BACKUP_ENCRYPTION_ENABLED", "true")
    assert pg_backup.encryption_required() is True


def _fake_tool(directory: Path, name: str = "pg_dump") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_find_tool_uses_path_when_pg_bin_is_unset(monkeypatch, tmp_path):
    """Regression: the backup image sets no PG_BIN. The old lookup checked
    relative paths only, so a valid pg_dump on PATH was reported missing."""
    tool = _fake_tool(tmp_path / "bin")
    monkeypatch.delenv("PG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tool.parent))
    assert pg_backup.find_tool("pg_dump") == tool


def test_find_tool_prefers_pg_bin_over_path(monkeypatch, tmp_path):
    pinned = _fake_tool(tmp_path / "pinned")
    _fake_tool(tmp_path / "other")
    monkeypatch.setenv("PG_BIN", str(pinned.parent))
    monkeypatch.setenv("PATH", str(tmp_path / "other"))
    assert pg_backup.find_tool("pg_dump") == pinned


def test_find_tool_never_searches_the_working_directory(monkeypatch, tmp_path):
    """A pg_dump planted in the current directory must never be selected."""
    _fake_tool(tmp_path / "cwd")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.delenv("PG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="Could not find pg_dump"):
        pg_backup.find_tool("pg_dump")


def test_restore_drill_resolves_psql_from_path_without_pg_bin(monkeypatch, tmp_path):
    """Regression: the drill had its own relative-only lookup, so with PG_BIN unset
    (the backup image) the restore failed with 'Could not find psql'."""
    from scripts import pg_restore_drill

    tool = _fake_tool(tmp_path / "bin", name="psql")
    monkeypatch.delenv("PG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tool.parent))
    assert pg_restore_drill.find_tool("psql") == tool
