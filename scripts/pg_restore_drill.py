"""
AUSTRO AI - Real PostgreSQL restore drill.

Restores a backup produced by scripts/pg_backup.py onto the configured
PostgreSQL instance: verifies integrity, drops & recreates the target database,
re-applies the SQL backup, then validates schema (table count) and row counts
against the recorded manifest. Finally re-connects through the application
DatabaseManager and confirms the application can query the restored DB.

Both plain gzip and AES-256-GCM encrypted archives are supported: the container
is detected from its version byte, decrypted, and gunzipped on the fly, so the
drill never needs the dump to fit in memory.

Usage:
    python scripts/pg_restore_drill.py <backup.sql.gz> <manifest.json>
    or:  python scripts/pg_restore_drill.py <backup.sql.gz>   (manifest next to it)

Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Force the PostgreSQL backend for the application-connection step. This must
# happen before the app modules are imported: they read settings at import time.
os.environ["DB_ENGINE"] = "postgresql"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.config.settings import settings  # noqa: E402
from app.database.connection import DatabaseManager  # noqa: E402
from scripts.pg_backup import (  # noqa: E402
    ARCHIVE_SUFFIX,
    MANIFEST_SUFFIX,
    archive_is_encrypted,
    file_sha256,
    open_archive_sql,
)

CHUNK = 1024 * 1024


def find_tool(name: str) -> Path:
    env = os.environ.get("PG_BIN", "")
    candidates = []
    if env:
        candidates.append(Path(env) / (name + ".exe"))
        candidates.append(Path(env) / name)
    candidates.append(Path(name))
    for c in candidates:
        if c.exists():
            return c
    raise RuntimeError(f"Could not find {name}. Set PG_BIN to the PostgreSQL bin directory.")


def dsn_env(superuser: bool = False):
    return {
        "PGHOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PGPORT": os.environ.get("DB_PORT", "5432"),
        "PGDATABASE": os.environ.get("DB_NAME", "austro_ai") if not superuser else "postgres",
        "PGUSER": "postgres" if superuser else os.environ.get("DB_USER", "austro"),
    }


def _run_psql(dbname: str, stream) -> "subprocess.CompletedProcess[bytes]":
    """Stream the decrypted SQL into psql without buffering it in memory."""
    env = {**os.environ, **dsn_env(superuser=False)}
    env["PGDATABASE"] = dbname
    env["PGCLIENTENCODING"] = "UTF8"
    psql = find_tool("psql")
    proc = subprocess.Popen(
        [str(psql), "-h", dsn_env(False)["PGHOST"], "-p", dsn_env(False)["PGPORT"],
         "-U", dsn_env(False)["PGUSER"], "-v", "ON_ERROR_STOP=1", "-d", dbname],
        stdin=stream, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = proc.communicate(timeout=600)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _recreate_database(psycopg2, isolation_autocommit, dbname: str, owner: str) -> None:
    adm = psycopg2.connect(
        host=dsn_env(True)["PGHOST"], port=dsn_env(True)["PGPORT"],
        dbname="postgres", user="postgres", password=os.environ.get("DB_PASSWORD", ""),
    )
    adm.set_isolation_level(isolation_autocommit)
    with adm.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{dbname}" OWNER "{owner}"')
    adm.close()


def main() -> int:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    if len(sys.argv) < 2:
        print(f"usage: pg_restore_drill.py <backup{ARCHIVE_SUFFIX}> [{MANIFEST_SUFFIX}]")
        return 1
    backup = Path(sys.argv[1])
    manifest_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if manifest_path is None:
        manifest_path = backup.with_name(backup.name.replace(ARCHIVE_SUFFIX, MANIFEST_SUFFIX))
    if not backup.exists() or not manifest_path.exists():
        print("backup or manifest not found")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db = os.environ.get("DB_NAME", "austro_ai")
    owner = os.environ.get("DB_USER", "austro")

    encrypted = archive_is_encrypted(backup)
    declared = bool((manifest.get("encryption") or {}).get("enabled"))
    if declared and not encrypted:
        print("integrity FAILED: manifest declares encryption but archive is not encrypted")
        return 1
    if encrypted and not declared:
        print("integrity FAILED: archive is encrypted but manifest does not declare it")
        return 1

    print(f"[1/6] Integrity check of {backup.name}"
          f"{' (encrypted)' if encrypted else ''}")
    expected_archive_sha = manifest.get("archive_sha256")
    if expected_archive_sha:
        actual_archive_sha = file_sha256(backup)
        if actual_archive_sha != expected_archive_sha:
            print("integrity FAILED: archive sha256 mismatch")
            return 1
        print("      archive sha256 matches manifest")

    # Stream the plaintext SQL through the same decrypt/decompress path the
    # backup uses, so the checksum proves the *content* survived encryption.
    digest = hashlib.sha256()
    try:
        with open_archive_sql(backup) as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
    except ValueError as exc:
        print(f"integrity FAILED: {exc}")
        return 1
    if digest.hexdigest() != manifest["checksum_sha256"]:
        print("integrity FAILED: content checksum mismatch")
        return 1
    print("      decompressed SQL sha256 matches manifest")

    print("[2/6] Verify SQL parses by applying to a disposable database")
    try_db = f"{db}_restore_probe"
    _recreate_database(psycopg2, ISOLATION_LEVEL_AUTOCOMMIT, try_db, owner)
    with open_archive_sql(backup) as stream:
        p = _run_psql(try_db, stream)
    if p.returncode != 0:
        print("probe apply FAILED:", p.stderr[-2000:].decode("utf-8", errors="replace"))
        return 1
    print("      disposable apply exit=0 (SQL backups cleanly)")

    print(f"[3/6] Drop + recreate target database {db}")
    _recreate_database(psycopg2, ISOLATION_LEVEL_AUTOCOMMIT, db, owner)
    print("      dropped + recreated")

    print(f"[4/6] Restore SQL into {db}")
    with open_archive_sql(backup) as stream:
        p = _run_psql(db, stream)
    if p.returncode != 0:
        print("restore FAILED:", p.stderr[-2000:].decode("utf-8", errors="replace"))
        return 1

    print("[5/6] Validate schema + row counts against manifest")
    c = psycopg2.connect(
        host=dsn_env(False)["PGHOST"], port=dsn_env(False)["PGPORT"], dbname=db,
        user=dsn_env(False)["PGUSER"], password=os.environ.get("DB_PASSWORD", ""),
    )
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
        )
        restored_tables = [r[0] for r in cur.fetchall()]
        restored_counts = {}
        for t in restored_tables:
            try:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                restored_counts[t] = int(cur.fetchone()[0])
            except Exception:
                restored_counts[t] = -1
    c.close()
    expected_tables = set(manifest["row_counts"].keys())
    if set(restored_tables) != expected_tables:
        missing = expected_tables - set(restored_tables)
        extra = set(restored_tables) - expected_tables
        raise AssertionError(f"table mismatch missing={missing} extra={extra}")
    mismatches = {
        t: (manifest["row_counts"].get(t), restored_counts.get(t))
        for t in expected_tables
        if manifest["row_counts"].get(t) != restored_counts.get(t)
    }
    if mismatches:
        raise AssertionError(f"row-count mismatches: {mismatches}")
    total = sum(v for v in restored_counts.values() if v > 0)
    print(f"      tables={len(restored_tables)} total_rows={total} ALL ROW COUNTS MATCH")

    print("[6/6] Application reconnect (DatabaseManager against restored DB)")
    # `settings` is a module-level singleton resolved at import time, so
    # mutating os.environ here would NOT repoint DatabaseManager. Fail loudly
    # instead of reporting a pass against the wrong database.
    if (settings.db_name or "austro_ai") != db:
        raise AssertionError(
            f"DatabaseManager would validate settings.db_name="
            f"{settings.db_name!r}, not the restored {db!r}"
        )
    mgr = DatabaseManager()
    conn = mgr._get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        n_tables = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM users")
        n_users = int(cur.fetchone()[0])
    conn.commit()
    mgr._close_connection()
    print(f"      app manager query OK: tables={n_tables}, users={n_users}")
    if n_users != manifest["row_counts"].get("users", n_users):
        raise AssertionError("users row count mismatch after app reconnect")

    print("\nRESTORE DRILL: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())