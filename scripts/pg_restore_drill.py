"""
AUSTRO AI - Real PostgreSQL restore drill.

Restores a backup produced by scripts/pg_backup.py onto the configured
PostgreSQL instance: verifies integrity, drops & recreates the target database,
re-applies the SQL backup, then validates schema (table count) and row counts
against the recorded manifest. Finally re-connects through the application
DatabaseManager and confirms the application can query the restored DB.

Usage:
    python scripts/pg_restore_drill.py <backup.sql.gz> <manifest.json>
    or:  python scripts/pg_restore_drill.py <backup.sql.gz>   (manifest next to it, _V)

Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Force the PostgreSQL backend for the application-connection step.
os.environ["DB_ENGINE"] = "postgresql"

REPO_ROOT = os.getcwd()
sys.path.insert(0, REPO_ROOT)

from app.config.settings import settings
from app.database.connection import DatabaseManager


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


def main() -> int:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    if len(sys.argv) < 2:
        print("usage: pg_restore_drill.py <backup.sql.gz> [manifest.json]")
        return 1
    backup = Path(sys.argv[1])
    manifest_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if manifest_path is None:
        manifest_path = Path(str(backup).replace(".sql.gz", ".manifest.json"))
    if not backup.exists() or not manifest_path.exists():
        print("backup or manifest not found")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db = os.environ.get("DB_NAME", "austro_ai")
    su_env = {**os.environ, **dsn_env(superuser=True)}
    su_env.pop("PGPASSWORD", None)

    print(f"[1/6] Integrity check of {backup.name}")
    data = gzip.open(str(backup), "rb").read()
    if hashlib.sha256(data).hexdigest() != manifest["checksum_sha256"]:
        print("integrity FAILED: checksum mismatch")
        return 1
    print("      sha256 matches manifest")

    print(f"[2/6] Verify SQL parses by applying to a disposable database")
    try_db = f"{db}_restore_probe"
    adm = psycopg2.connect(
        host=dsn_env(True)["PGHOST"], port=dsn_env(True)["PGPORT"],
        dbname="postgres", user="postgres", password=os.environ.get("DB_PASSWORD", ""),
    )
    adm.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with adm.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {try_db} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {try_db} OWNER {os.environ.get('DB_USER', 'austro')}")
    adm.close()
    probe_env = {**os.environ, **dsn_env(superuser=False)}
    probe_env["PGDATABASE"] = try_db
    probe_env["PGCLIENTENCODING"] = "UTF8"
    psql = find_tool("psql")
    p = subprocess.run(
        [str(psql), "-h", dsn_env(False)["PGHOST"], "-p", dsn_env(False)["PGPORT"],
         "-U", dsn_env(False)["PGUSER"], "-v", "ON_ERROR_STOP=1", "-d", try_db],
        input=data, env=probe_env,
        capture_output=True, text=False, timeout=600,
    )
    if p.returncode != 0:
        print("probe apply FAILED:", p.stderr[-2000:].decode("utf-8", errors="replace"))
        return 1
    print("      disposable apply exit=0 (SQL backups cleanly)")

    print(f"[3/6] Drop + recreate target database {db}")
    adm = psycopg2.connect(
        host=dsn_env(True)["PGHOST"], port=dsn_env(True)["PGPORT"],
        dbname="postgres", user="postgres", password=os.environ.get("DB_PASSWORD", ""),
    )
    adm.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with adm.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {db} OWNER {os.environ.get('DB_USER', 'austro')}")
    adm.close()
    print("      dropped + recreated")

    print(f"[4/6] Restore SQL into {db}")
    rest_env = {**os.environ, **dsn_env(superuser=False)}
    rest_env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run(
        [str(psql), "-h", dsn_env(False)["PGHOST"], "-p", dsn_env(False)["PGPORT"],
         "-U", dsn_env(False)["PGUSER"], "-v", "ON_ERROR_STOP=1", "-d", db],
        input=data, env=rest_env,
        capture_output=True, text=False, timeout=600,
    )
    if p.returncode != 0:
        print("restore FAILED:", p.stderr[-2000:].decode("utf-8", errors="replace"))
        return 1

    print(f"[5/6] Validate schema + row counts against manifest")
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

    print(f"[6/6] Application reconnect (DatabaseManager against restored DB)")
    # repoint the DB we validate against to the restored database
    os.environ["DB_NAME"] = db
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