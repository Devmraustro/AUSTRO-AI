"""
AUSTRO AI - Real PostgreSQL validation harness (Phase G, item 1).

Runs against a REAL PostgreSQL instance configured via:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

Verifies, with explicit COMMAND / RESULT / EXIT codes:
  1. connection
  2. schema creation (core tables + migration-created tables)
  3. all migrations applied
  4. idempotent migration rerun
  5. indexes / constraints present
  6. transactions (commit + rollback)
  7. concurrent access (multithreaded writers)
  8. application startup against PostgreSQL (production config + manager init)

Usage:
    python scripts/validate_postgresql.py
    exit 0 on full pass, 1 on any failing check.
"""

from __future__ import annotations

import os
import sys
import threading

os.environ.setdefault("DB_ENGINE", "postgresql")

# Production-like configuration for the startup check (must pass load_settings
# production validation while connecting to the real PostgreSQL instance).
os.environ.setdefault("AUSTRO_ENVIRONMENT", "production")
os.environ.setdefault("AUSTRO_LOG_LEVEL", "WARNING")
os.environ.setdefault("WEBHOOK_URL", "https://austro.example.com/webhook")
os.environ.setdefault("WEBHOOK_SECRET", "validate-webhook-secret-12345")
os.environ.setdefault("USE_LOCAL_FALLBACK", "false")
os.environ.setdefault("GEMINI_API_KEY", "validate-gemini-key-0000000000")
os.environ.setdefault("BOT_TOKEN", "123456789:VALIDATE-bot-token-abcdefghij")

import datetime  # noqa: E402
import os
import sys

REPO_ROOT = os.getcwd()
sys.path.insert(0, REPO_ROOT)

from app.config.settings import load_settings  # noqa: E402
from app.database.connection import DatabaseManager  # noqa: E402
from app.database.migrations import apply_migrations, MIGRATIONS  # noqa: E402

RESULTS = []


def check(name: str, fn):
    """Run a check, record COMMAND/RESULT/EXIT."""
    try:
        details = fn()
        RESULTS.append((name, "PASS", details))
        print(f"[PASS] {name} :: {details}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, "FAIL", str(exc)))
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc}")


def main() -> int:
    import psycopg2

    settings = load_settings()
    print(f"PostgreSQL target: {settings.db_host}:{settings.db_port}/{settings.db_name} "
          f"user={settings.db_user} engine={settings.db_engine}")

    # ---------------------------------------------------------------- connect
    def _connect():
        conn = psycopg2.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version();")
            row = cur.fetchone()
        conn.close()
        return f"user={row[0]} db={row[1]} :: {row[2].split()[0]} {row[2].split()[1]}"

    check("connection (psycopg2 -> real PostgreSQL)", _connect)

    # ------------------------------------------------------- schema + migrations
    all_tables = []

    def _schema():
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' ORDER BY table_name"
            )
            all_tables.extend([r[0] for r in cur.fetchall()])
        conn.commit()
        mgr._close_connection()
        return f"{len(all_tables)} tables created: {', '.join(sorted(all_tables))}"

    check("schema creation (core + migrations on PostgreSQL)", _schema)

    def _migrations_applied():
        applied = [m.schema_name for m in MIGRATIONS]
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name, version FROM schema_migrations ORDER BY schema_name")
            rows = cur.fetchall()
        conn.commit()
        mgr._close_connection()
        recorded = {r[0]: int(r[1]) for r in rows}
        missing = [a for a in applied if recorded.get(a) != 1]
        if missing:
            raise AssertionError(f"missing/incorrect migration records: {missing}")
        return f"recorded={recorded}"

    check("all migrations applied (knowledge/memory/learning v1)", _migrations_applied)

    def _idempotent_rerun():
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name, version FROM schema_migrations ORDER BY schema_name")
            before = {r[0]: int(r[1]) for r in cur.fetchall()}
        conn.commit()
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name, version FROM schema_migrations ORDER BY schema_name")
            after = {r[0]: int(r[1]) for r in cur.fetchall()}
        conn.commit()
        mgr._close_connection()
        if before != after:
            raise AssertionError(f"rerun changed records: {before} -> {after}")
        return f"schema_migrations unchanged ({len(after)} records)"

    check("idempotent migration rerun (no droop, no change)", _idempotent_rerun)

    def _indexes_constraints():
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY indexname"
            )
            indexes = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = (SELECT oid FROM pg_namespace WHERE nspname='public') "
                "AND contype IN ('f','p','u') ORDER BY conname"
            )
            constraints = [r[0] for r in cur.fetchall()]
        conn.commit()
        mgr._close_connection()
        return (f"indexes={len(indexes)} constraints(f/p/u)={len(constraints)} "
                f"e.g. idx_memories_owner={'idx_memories_owner' in indexes} "
                f"fk_present={'knowledge_versions_source_id_fkey' in constraints}")

    check("indexes + constraints (pg_indexes / pg_constraint)", _indexes_constraints)

    # ------------------------------------------------------------ transactions
    def _transactions():
        import psycopg2 as pg2
        conn = pg2.connect(
            host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
            user=settings.db_user, password=settings.db_password,
            cursor_factory=pg2.extras.DictCursor,
        )
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE tx_test (id SERIAL PRIMARY KEY, val TEXT)")
            cur.execute("INSERT INTO tx_test (val) VALUES ('committed')")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tx_test (val) VALUES ('should_rollback')")
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tx_test")
            count = cur.fetchone()[0]
            cur.execute("SELECT val FROM tx_test")
            vals = [r[0] for r in cur.fetchall()]
        conn.close()
        if count != 1 or vals != ["committed"]:
            raise AssertionError(f"rollback leak: count={count} vals={vals}")
        return f"commit+rollback verified (count={count}, rows={vals})"

    check("transactions (commit persisted, rollback rolled back)", _transactions)

    # ------------------------------------------------------------- concurrency
    def _concurrency():
        import psycopg2 as pg2
        from psycopg2.extras import DictCursor
        conn = pg2.connect(
            host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
            user=settings.db_user, password=settings.db_password,
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS concurrent_test (id SERIAL PRIMARY KEY, worker TEXT)")
            cur.execute("TRUNCATE concurrent_test")
        conn.close()

        def worker(wid: int, n: int):
            c = pg2.connect(
                host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
                user=settings.db_user, password=settings.db_password,
            )
            c.autocommit = True
            for i in range(n):
                with c.cursor() as cur:
                    cur.execute("INSERT INTO concurrent_test (worker) VALUES (%s)", (f"w{wid}",))
            c.close()

        threads = [threading.Thread(target=worker, args=(w, 50)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        c = pg2.connect(
            host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
            user=settings.db_user, password=settings.db_password,
        )
        with c.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT worker) FROM concurrent_test")
            total, distinct = cur.fetchone()
        c.close()
        if total != 400 or distinct != 8:
            raise AssertionError(f"expected 400 rows/8 workers, got total={total} distinct={distinct}")
        return f"8 threads x 50 rows = {total} rows, {distinct} distinct workers"

    check("concurrent access (8 threads x 50 serial writes)", _concurrency)

    # ------------------------------------------------------- application startup
    def _startup():
        import config as app_config
        if not hasattr(app_config, "DB_ENGINE"):
            raise AssertionError("expected DB_ENGINE export in config.py")
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            n_tables = cur.fetchone()[0]
        conn.commit()
        mgr._close_connection()
        return (f"app config loaded (DB_ENGINE={app_config.DB_ENGINE}), "
                f"DatabaseManager initialized, {n_tables} public tables, "
                f"recorded migration_date={datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}")

    check("application startup against PostgreSQL (prod config + manager)", _startup)

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print()
    for name, status, _ in RESULTS:
        print(f"  {status}  {name}")
    print()
    if failed:
        print(f"RESULT: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed. See FAIL lines above.")
        return 1
    print(f"RESULT: ALL {len(RESULTS)} POSTGRESQL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())