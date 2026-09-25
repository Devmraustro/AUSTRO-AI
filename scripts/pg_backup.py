"""
AUSTRO AI - Real PostgreSQL backup (pg_dump) drill.

Executes a real logical backup against the configured PostgreSQL instance using
the pg_dump binary, compresses it, records a manifest (sha256 + per-table row
counts) and verifies archive integrity.

Usage:
    python scripts/pg_backup.py [outdir]
    env: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, PG_BIN (optional)

Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import datetime
from pathlib import Path


def pg_bin_dir() -> str:
    return os.environ.get("PG_BIN", "")


def find_tool(name: str) -> Path:
    env = os.environ.get("PG_BIN", "")
    candidates = []
    if env:
        candidates.append(Path(env) / (name + ".exe"))
        candidates.append(Path(env) / name)
    candidates.append(Path(".") / name)
    candidates.append(Path(name))  # on PATH
    for c in candidates:
        if c.exists():
            return c
    raise RuntimeError(
        f"Could not find {name}. Set PG_BIN to the PostgreSQL bin directory "
        f"(e.g. C:\\...\\pgsql\\bin)."
    )


def dsn_env():
    return {
        "PGHOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PGPORT": os.environ.get("DB_PORT", "5432"),
        "PGDATABASE": os.environ.get("DB_NAME", "austro_ai"),
        "PGUSER": os.environ.get("DB_USER", "austro"),
    }


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backups")
    outdir.mkdir(parents=True, exist_ok=True)

    import psycopg2

    db = os.environ.get("DB_NAME", "austro_ai")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = outdir / f"austro_ai_backup_{stamp}"
    raw = Path(str(base) + ".sql")
    zipped = Path(str(base) + ".sql.gz")

    print(f"[1/5] Row-count snapshot before backup")
    conn = psycopg2.connect(
        host=dsn_env()["PGHOST"], port=dsn_env()["PGPORT"], dbname=db,
        user=dsn_env()["PGUSER"], password=os.environ.get("DB_PASSWORD", ""),
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
        )
        tables = [r[0] for r in cur.fetchall()]
        counts = {}
        for t in tables:
            try:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                counts[t] = int(cur.fetchone()[0])
            except Exception:  # skip if unreadable (e.g. concurrent_test scratch)
                counts[t] = -1
    conn.close()
    print(f"      {len(tables)} tables, total rows={sum(v for v in counts.values() if v > 0)}")

    print(f"[2/5] pg_dump -> {raw.name}")
    pg_dump = find_tool("pg_dump")
    env = {**os.environ, **dsn_env()}
    env.pop("PGPASSWORD", None)
    p = subprocess.run(
        [str(pg_dump), "-h", dsn_env()["PGHOST"], "-p", dsn_env()["PGPORT"],
         "-U", dsn_env()["PGUSER"], "-d", db, "--no-owner", "--no-acl",
         "--clean", "--if-exists", "-f", str(raw)],
        env=env, capture_output=True, text=True, timeout=300,
    )
    if p.returncode != 0:
        print("pg_dump failed:", p.stderr)
        return 1
    print(f"      dump exit=0, size={raw.stat().st_size} bytes")

    print(f"[3/5] Compress + manifest")
    with open(raw, "rb") as f:
        data = f.read()
    with gzip.open(str(zipped), "wb", compresslevel=6) as gz:
        gz.write(data)
    sha = hashlib.sha256(data).hexdigest()
    manifest = {
        "backup": zipped.name,
        "checksum_sha256": sha,
        "created_utc": stamp,
        "engine": "postgresql",
        "tables": len(tables),
        "row_counts": counts,
    }
    mp = outdir / f"austro_ai_backup_{stamp}.manifest.json"
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.remove(raw)
    print(f"      gzip size={zipped.stat().st_size} bytes, sha256={sha[:16]}...")

    print(f"[4/5] Integrity: gzip test")
    try:
        with gzip.open(str(zipped), "rb") as gz:
            out = gz.read()
        if hashlib.sha256(out).hexdigest() != sha:
            raise AssertionError("sha256 mismatch after round-trip")
        print("      gzip decompress + sha256 round-trip OK")
    except Exception as exc:
        print("intregrity FAILED:", exc)
        return 1

    print(f"[5/5] Backup complete: {zipped} (+ manifest {mp.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())