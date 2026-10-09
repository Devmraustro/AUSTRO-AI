"""Disposable database fixtures for the CI backup/restore integration job.

Used ONLY by .github/workflows/ci.yml against the ephemeral `postgres:16`
service container that the job itself starts. It never runs against a
developer's database and never reads a production credential.

Subcommands:
  seed --expected FILE     create the source DB content, the restore target with
                           a sentinel table, and the application role; write the
                           expected table->row-count map to FILE.
  sentinel --target DB --expect present|absent
                           assert whether the restore target still holds the
                           pre-restore sentinel (proves no destructive change).
  counts --database DB --expected FILE
                           assert the table set and row counts of DB, as the
                           administrative role.
  verify-restored --target DB --expected FILE
                           assert the restored DB: exact table set, row counts,
                           sentinel gone, and the application role can connect.
  verify-artifact --archive FILE --manifest FILE --key FILE
                           assert the published archive is encrypted (AES-256-GCM
                           container), contains no plaintext SQL, and that the
                           manifest declares encryption while containing no key
                           material in any encoding.

Credentials come from environment variables only:
  CI_DB_HOST (default 127.0.0.1), CI_DB_PORT (default 5432),
  CI_DB_ADMIN_USER, CI_DB_ADMIN_PASSWORD, CI_APP_ROLE, CI_APP_PASSWORD,
  CI_SOURCE_DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict

import psycopg2
from psycopg2 import sql

SENTINEL_TABLE = "restore_sentinel"
SOURCE_ROWS = {
    "users": ["alice", "bob", "carol"],
    "goals": ["learn rust", "run 5k", "read 12 books", "save money",
              "sleep 8h", "cook weekly", "learn piano"],
    "memories": ["likes tea", "lives in Lisbon", "works nights", "has a cat",
                 "birthday in May"],
}


def _conn(dbname: str, *, user: str, password: str):
    conn = psycopg2.connect(
        host=os.environ.get("CI_DB_HOST", "127.0.0.1"),
        port=os.environ.get("CI_DB_PORT", "5432"),
        dbname=dbname, user=user, password=password,
    )
    conn.autocommit = True
    return conn


def _admin(dbname: str):
    return _conn(dbname, user=os.environ["CI_DB_ADMIN_USER"],
                 password=os.environ["CI_DB_ADMIN_PASSWORD"])


def _app(dbname: str):
    return _conn(dbname, user=os.environ["CI_APP_ROLE"],
                 password=os.environ["CI_APP_PASSWORD"])


def _table_names(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' "
                    "ORDER BY table_name")
        return [r[0] for r in cur.fetchall()]


def _counts(conn) -> Dict[str, int]:
    out: Dict[str, int] = {}
    with conn.cursor() as cur:
        for table in _table_names(conn):
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            out[table] = int(cur.fetchone()[0])
    return out


def cmd_seed(args) -> int:
    source = os.environ["CI_SOURCE_DB"]
    target = os.environ["CI_RESTORE_TARGET"]
    app_role = os.environ["CI_APP_ROLE"]
    app_password = os.environ["CI_APP_PASSWORD"]

    admin = _admin("postgres")
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(
                sql.Identifier(app_role)), (app_password,))
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(target)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
    admin.close()

    src = _admin(source)
    with src.cursor() as cur:
        for table, rows in SOURCE_ROWS.items():
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table)))
            cur.execute(sql.SQL("CREATE TABLE {} (id serial PRIMARY KEY, body text NOT NULL)")
                        .format(sql.Identifier(table)))
            for row in rows:
                cur.execute(sql.SQL("INSERT INTO {} (body) VALUES (%s)")
                            .format(sql.Identifier(table)), (row,))
    expected = _counts(src)
    src.close()

    tgt = _admin(target)
    with tgt.cursor() as cur:
        cur.execute(sql.SQL("CREATE TABLE {} (marker text)").format(
            sql.Identifier(SENTINEL_TABLE)))
        cur.execute(sql.SQL("INSERT INTO {} VALUES ('pre-existing-target-data')").format(
            sql.Identifier(SENTINEL_TABLE)))
    tgt.close()

    with open(args.expected, "w", encoding="utf-8") as handle:
        json.dump({"row_counts": expected}, handle, indent=2, sort_keys=True)
    print(f"seeded source={source} tables={sorted(expected)} rows={expected}")
    print(f"restore target {target} created with sentinel table '{SENTINEL_TABLE}'")
    return 0


def _load_expected(path: str) -> Dict[str, int]:
    with open(path, encoding="utf-8") as handle:
        return dict(json.load(handle)["row_counts"])


def cmd_sentinel(args) -> int:
    conn = _admin(args.target)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s", (SENTINEL_TABLE,))
        present = cur.fetchone() is not None
    conn.close()
    if present != (args.expect == "present"):
        print(f"sentinel check FAILED: expected {args.expect}, found "
              f"{'present' if present else 'absent'} in {args.target}")
        return 1
    print(f"sentinel check OK: {SENTINEL_TABLE} is {args.expect} in {args.target}")
    return 0


def cmd_counts(args) -> int:
    conn = _admin(args.database)
    actual = _counts(conn)
    conn.close()
    expected = _load_expected(args.expected)
    if actual != expected:
        print(f"count check FAILED for {args.database}: expected={expected} actual={actual}")
        return 1
    print(f"count check OK for {args.database}: {actual}")
    return 0


def cmd_verify_restored(args) -> int:
    expected = _load_expected(args.expected)
    admin = _admin(args.target)
    actual = _counts(admin)
    admin.close()
    if set(actual) != set(expected):
        print(f"table set FAILED: missing={sorted(set(expected) - set(actual))} "
              f"extra={sorted(set(actual) - set(expected))}")
        return 1
    if actual != expected:
        print(f"row counts FAILED: expected={expected} actual={actual}")
        return 1
    if SENTINEL_TABLE in actual:
        print("sentinel FAILED: pre-restore data survived a full restore")
        return 1
    app = _app(args.target)
    with app.cursor() as cur:
        cur.execute("SELECT current_user, count(*) FROM users")
        user, n = cur.fetchone()
    app.close()
    print(f"restored DB verified: tables={sorted(actual)} rows={actual} "
          f"app_role={user} users={n}")
    return 0


def cmd_verify_artifact(args) -> int:
    import base64
    import gzip

    with open(args.archive, "rb") as handle:
        blob = handle.read()
    with open(args.manifest, "rb") as handle:
        manifest_bytes = handle.read()
    with open(args.key, "rb") as handle:
        key = handle.read()
    problems = []
    if blob[:1] != b"\x01":
        problems.append("archive does not start with the encrypted-container version byte")
    if blob[:2] == b"\x1f\x8b":
        problems.append("archive is a plain gzip member (unencrypted)")
    for marker in (b"CREATE TABLE", b"INSERT INTO", b"PostgreSQL database dump"):
        if marker in blob:
            problems.append(f"plaintext SQL marker {marker!r} found in archive")
    try:
        gzip.decompress(blob)
        problems.append("archive decompresses WITHOUT a key (plaintext leak)")
    except (OSError, EOFError, ValueError):
        pass
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if (manifest.get("encryption") or {}).get("enabled") is not True:
        problems.append("manifest does not declare encryption.enabled=true")
    if (manifest.get("encryption") or {}).get("algo") != "aes-256-gcm":
        problems.append("manifest does not declare aes-256-gcm")
    needles = {
        "raw key": key,
        "hex key": key.hex().encode(),
        "base64 key": base64.b64encode(key),
        "base64url key": base64.urlsafe_b64encode(key),
    }
    for label, needle in needles.items():
        if needle and needle in manifest_bytes:
            problems.append(f"manifest contains {label} material")
        if needle and needle in blob:
            problems.append(f"archive contains {label} material")
    if args.outdir:
        entries = sorted(os.listdir(args.outdir))
        expected_entries = sorted({os.path.basename(args.archive),
                                   os.path.basename(args.manifest)})
        if entries != expected_entries:
            problems.append(f"backup directory holds unexpected entries: {entries}")
    if problems:
        for problem in problems:
            print(f"artifact check FAILED: {problem}")
        return 1
    print(f"artifact check OK: {os.path.basename(args.archive)} is an AES-256-GCM "
          f"container ({len(blob)} bytes), no plaintext SQL, manifest declares "
          f"{manifest['encryption']['algo']} with no key material")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    subs = parser.add_subparsers(dest="cmd", required=True)
    p = subs.add_parser("seed")
    p.add_argument("--expected", required=True)
    p.set_defaults(func=cmd_seed)
    p = subs.add_parser("sentinel")
    p.add_argument("--target", required=True)
    p.add_argument("--expect", choices=["present", "absent"], required=True)
    p.set_defaults(func=cmd_sentinel)
    p = subs.add_parser("counts")
    p.add_argument("--database", required=True)
    p.add_argument("--expected", required=True)
    p.set_defaults(func=cmd_counts)
    p = subs.add_parser("verify-restored")
    p.add_argument("--target", required=True)
    p.add_argument("--expected", required=True)
    p.set_defaults(func=cmd_verify_restored)
    p = subs.add_parser("verify-artifact")
    p.add_argument("--archive", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--outdir", default=None,
                   help="if given, the directory must contain exactly the archive and manifest")
    p.set_defaults(func=cmd_verify_artifact)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
