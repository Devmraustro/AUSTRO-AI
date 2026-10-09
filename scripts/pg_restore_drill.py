"""AUSTRO AI - PostgreSQL restore drill (DESTRUCTIVE, disposable target only).

Restores a backup produced by scripts/pg_backup.py into an EXPLICIT, disposable
target database and verifies it. Plain gzip and AES-256-GCM encrypted archives
are both supported.

Execution order (each step fails closed before the next):

  1. Refuse unless every safety rule in scripts/restore_safety.py passes:
     explicit RESTORE_TARGET_DB (must contain "restore", must not be DB_NAME),
     exact RESTORE_CONFIRM_DESTRUCTIVE=DROP-AND-RESTORE:<target>, and explicit
     RESTORE_ADMIN_USER / RESTORE_ADMIN_PASSWORD. No database is touched.
  2. Authenticate the archive end to end: manifest/archive checksums, key
     validation and decryption, decompression, and content SHA-256. No database
     is touched. A wrong key or any tampered byte stops the drill here.
  3. Drop and recreate the target (owner DB_USER) as the administrative role.
  4. Stream the verified SQL into the target with psql (ON_ERROR_STOP=1) as the
     administrative role. Grant the application role access when it differs.
  5. Validate table set, row counts and connectivity (as the application role)
     against the manifest.

Usage:
    RESTORE_TARGET_DB=austro_ai_restore_drill \\
    RESTORE_CONFIRM_DESTRUCTIVE=DROP-AND-RESTORE:austro_ai_restore_drill \\
    RESTORE_ADMIN_USER=... RESTORE_ADMIN_PASSWORD=... \\
    python scripts/pg_restore_drill.py <backup.sql.gz> [manifest.json]

Exit code 0 on success, non-zero on failure. Exit code 2 means a safety rule
refused the request before any archive or database work was done.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.pg_backup import (  # noqa: E402
    ARCHIVE_SUFFIX,
    MANIFEST_SUFFIX,
    archive_is_encrypted,
    file_sha256,
    open_archive_sql,
    write_pgpass,
)
from scripts.restore_safety import (  # noqa: E402
    RestoreRefused,
    validate_restore_request,
    verify_backup_before_restore,
)

CHUNK = 1024 * 1024
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


def find_tool(name: str) -> Path:
    env = os.environ.get("PG_BIN", "")
    candidates = []
    if env:
        candidates.append(Path(env) / (name + ".exe"))
        candidates.append(Path(env) / name)
    candidates.append(Path(name))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find {name}. Set PG_BIN to the PostgreSQL bin directory.")


def _say(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def _psycopg2_connect(**kwargs):
    import psycopg2  # imported lazily so pure helpers stay import-safe

    return psycopg2.connect(**kwargs)


def _pg_conn_args(env: Mapping[str, str]) -> Dict[str, str]:
    return {
        "host": env.get("DB_HOST", "127.0.0.1"),
        "port": env.get("DB_PORT", "5432"),
    }


def _run_psql(
    dbname: str,
    stream,
    *,
    env: Mapping[str, str],
    user: str,
    pgpass: Path,
    runner: Optional[Callable[..., "subprocess.CompletedProcess[bytes]"]] = None,
) -> "subprocess.CompletedProcess[bytes]":
    """Stream SQL into psql. The password reaches libpq only through PGPASSFILE."""
    if runner is not None:
        return runner(dbname, stream)
    conn = _pg_conn_args(env)
    psql_env = {
        **{k: v for k, v in os.environ.items() if k not in ("PGPASSWORD",)},
        "PGHOST": conn["host"],
        "PGPORT": conn["port"],
        "PGDATABASE": dbname,
        "PGUSER": user,
        "PGPASSFILE": str(pgpass),
        "PGCLIENTENCODING": "UTF8",
    }
    # psql reads the verified SQL from its stdin pipe. The decrypted stream is a
    # Python object without a file descriptor, so it is copied in bounded
    # chunks. stderr goes to a temp file so a noisy psql cannot deadlock on a
    # full pipe while we are still writing stdin.
    with tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            [str(find_tool("psql")), "-h", conn["host"], "-p", conn["port"],
             "-U", user, "-v", "ON_ERROR_STOP=1", "-d", dbname, "--no-password"],
            stdin=subprocess.PIPE, env=psql_env,
            stdout=subprocess.DEVNULL, stderr=err_file,
        )
        try:
            for block in iter(lambda: stream.read(CHUNK), b""):
                proc.stdin.write(block)
        except BrokenPipeError:
            pass  # psql exited early; its exit code and stderr decide the outcome
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        returncode = proc.wait(timeout=1800)
        err_file.seek(0)
        err = err_file.read()
    return subprocess.CompletedProcess(proc.args, returncode, b"", err)


def _recreate_database(conn_factory, target: str, owner: str) -> None:
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    admin = conn_factory(dbname="postgres")
    try:
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                        .format(sql.Identifier(target)))
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}")
                        .format(sql.Identifier(target), sql.Identifier(owner)))
    finally:
        admin.close()


def _grant_application_role(conn_factory, target: str, app_role: str) -> None:
    from psycopg2 import sql

    conn = conn_factory(dbname=target)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(target), sql.Identifier(app_role)))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(app_role)))
            cur.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(sql.Identifier(app_role)))
            cur.execute(sql.SQL(
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(app_role)))
    finally:
        conn.close()


def _validate_restored(conn_factory, manifest: Mapping, target: str) -> Tuple[int, int]:
    """Table set, row counts and connectivity as the application role."""
    conn = conn_factory(dbname=target)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone()[0] != 1:
                raise AssertionError("connectivity probe returned an unexpected value")
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
            )
            restored_tables = [r[0] for r in cur.fetchall()]
            restored_counts: Dict[str, int] = {}
            for table in restored_tables:
                cur.execute(f'SELECT count(*) FROM "{table}"')
                restored_counts[table] = int(cur.fetchone()[0])
    finally:
        conn.close()

    expected = dict(manifest.get("row_counts") or {})
    if set(restored_tables) != set(expected):
        raise AssertionError(
            f"table mismatch missing={sorted(set(expected) - set(restored_tables))} "
            f"extra={sorted(set(restored_tables) - set(expected))}"
        )
    mismatches = {
        t: (expected[t], restored_counts.get(t))
        for t in expected
        if expected[t] != restored_counts.get(t)
    }
    if mismatches:
        raise AssertionError(f"row-count mismatches: {mismatches}")
    total = sum(v for v in restored_counts.values() if v > 0)
    return len(restored_tables), total


def run_restore_drill(
    backup: Path,
    manifest_path: Path,
    env: Optional[Mapping[str, str]] = None,
    *,
    connect: Optional[Callable[..., object]] = None,
    psql_runner: Optional[Callable[..., object]] = None,
) -> int:
    env = dict(os.environ if env is None else env)
    connect = connect or _psycopg2_connect
    app_db = env.get("DB_NAME") or None
    app_role = env.get("DB_USER", "austro")

    # 1. Safety rules (no I/O against the database or the archive).
    try:
        target, admin_user, admin_password = validate_restore_request(
            env, app_db=app_db
        )
    except RestoreRefused as exc:
        _say("refused", f"{exc}. No archive or database work was performed.")
        return EXIT_REFUSED

    if not backup.is_file() or not manifest_path.is_file():
        _say("refused", "backup or manifest not found. Nothing was changed.")
        return EXIT_REFUSED
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 2. Archive authentication BEFORE any destructive database operation.
    _say("1/5", f"authenticating {backup.name}"
                f"{' (encrypted)' if archive_is_encrypted(backup) else ''}")
    try:
        verify_backup_before_restore(
            backup, manifest,
            archive_is_encrypted=archive_is_encrypted,
            file_sha256=file_sha256,
            open_sql=open_archive_sql,
            chunk=CHUNK,
        )
    except Exception as exc:  # noqa: BLE001 - IntegrityError, missing/invalid key, I/O
        _say("refused", f"integrity FAILED: {type(exc).__name__}: {exc}. "
                        "The target database was NOT modified.")
        return EXIT_FAILED
    _say("1/5", "archive, key, decompression and content checksum verified")

    def conn_factory(dbname: str, *, user: str = admin_user, password: str = admin_password):
        args = _pg_conn_args(env)
        return connect(dbname=dbname, user=user, password=password, **args)

    with tempfile.TemporaryDirectory(prefix="austro-restore-") as tmp:
        pgpass = write_pgpass(Path(tmp), env.get("DB_HOST", "127.0.0.1"),
                              env.get("DB_PORT", "5432"), "*", admin_user, admin_password)
        try:
            _say("2/5", f"drop and recreate target '{target}' (owner {app_role})")
            _recreate_database(conn_factory, target, app_role)

            _say("3/5", f"restore verified SQL into '{target}' as administrative role")
            with open_archive_sql(backup) as stream:
                proc = _run_psql(target, stream, env=env, user=admin_user,
                                 pgpass=pgpass, runner=psql_runner)
            if proc.returncode != 0:
                err = (proc.stderr or b"")
                if isinstance(err, bytes):
                    err = err.decode("utf-8", errors="replace")
                _say("error", f"restore FAILED (psql exit={proc.returncode}): {err[-800:]}")
                return EXIT_FAILED

            if app_role and app_role != admin_user:
                _say("3/5", f"grant application role '{app_role}' access")
                _grant_application_role(conn_factory, target, app_role)

            _say("4/5", "validate table set, row counts and connectivity")
            tables, total = _validate_restore_as_app(
                connect, env, manifest, target, app_role
            )
            _say("4/5", f"tables={tables} total_rows={total} ALL ROW COUNTS MATCH")
        except Exception as exc:  # noqa: BLE001 - any failure is reported, fail closed
            _say("error", f"restore drill FAILED: {type(exc).__name__}: {exc}")
            return EXIT_FAILED

    _say("5/5", "RESTORE DRILL: ALL CHECKS PASSED")
    return EXIT_OK


def _validate_restore_as_app(connect, env, manifest, target, app_role):
    password = env.get("DB_PASSWORD", "")

    def app_factory(dbname: str):
        return connect(dbname=dbname, user=app_role, password=password,
                       **_pg_conn_args(env))

    return _validate_restored(app_factory, manifest, target)


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(f"usage: pg_restore_drill.py <backup{ARCHIVE_SUFFIX}> [{MANIFEST_SUFFIX}]")
        return EXIT_REFUSED
    backup = Path(args[0])
    manifest_path = (Path(args[1]) if len(args) > 1 else
                     backup.with_name(backup.name.replace(ARCHIVE_SUFFIX, MANIFEST_SUFFIX)))
    return run_restore_drill(backup, manifest_path)


if __name__ == "__main__":
    sys.exit(main())
