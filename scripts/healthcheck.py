"""
AUSTRO AI - Container / process readiness health check.

Exit 0 when the application is ready to serve traffic:
- configuration loads and BOT_TOKEN present
- database (PostgreSQL or SQLite) reachable and queryable
- knowledge storage directory writable
- logs directory writable
Exit 1 (with reason on stdout/stderr) otherwise.

Used by the Dockerfile HEALTHCHECK and by PRODUCTION_SMOKE_TEST.
"""

from __future__ import annotations

import os
import sys
import tempfile

REPO_ROOT = os.getcwd()
sys.path.insert(0, REPO_ROOT)


def fail(msg: str) -> int:
    print(f"UNHEALTHY: {msg}")
    return 1


def main() -> int:
    # 1. config
    try:
        from app.config.settings import settings
    except Exception as exc:  # noqa: BLE001
        return fail(f"config load failed: {exc}")
    if not settings.bot_token:
        return fail("BOT_TOKEN missing")

    # 2. database reachable
    try:
        if settings.db_engine == "postgresql":
            import psycopg2

            conn = psycopg2.connect(
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_password,
                connect_timeout=5,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.close()
        else:
            import sqlite3

            conn = sqlite3.connect(settings.db_path, timeout=5)
            conn.execute("SELECT 1").fetchone()
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return fail(f"database not reachable: {exc}")

    # 3. storage + logs writable
    for path, label in ((settings.knowledge_storage_path, "knowledge_storage"),
                        (settings.logs_path, "logs")):
        try:
            os.makedirs(path, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path, prefix=".health_")
            os.close(fd)
            os.remove(tmp)
        except Exception as exc:  # noqa: BLE001
            return fail(f"{label} not writable: {exc}")

    print("HEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())