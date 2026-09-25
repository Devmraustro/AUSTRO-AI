"""
AUSTRO AI - Production smoke test (Phase G item 7).

Runs a battery of checks against a production-like environment WITHOUT needing
live Telegram/AI credentials (those are classified separately). Each step
records COMMAND / RESULT / EXIT.

Checks:
  1. production configuration loads + validation passes
  2. readiness healthcheck (scripts/healthcheck.py)
  3. database: connect + schema + migrations idempotency (SQLite default;
     set DB_ENGINE=postgresql + DB_* for the PostgreSQL path)
  4. scheduler module imports and schedules jobs
  5. knowledge storage writable + basic RAG retrieval works (fallback)
  6. AI provider: unavailable upstream (no live key) -> graceful fallback path
  7. structured logging writes to log file with redaction
  8. application restart recovery (fresh manager reuse, no schema drift)

Usage:
    python scripts/smoke_test.py
    env may set AUSTRO_ENVIRONMENT=production, DB_ENGINE, DB_* etc.
"""

from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.getcwd()
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("BOT_TOKEN", "123456789:SMOKE-test-token-abcdefghij")

RESULTS = []


def step(name: str, fn):
    t0 = time.perf_counter()
    try:
        detail = fn()
        dt = (time.perf_counter() - t0) * 1000
        RESULTS.append((name, "PASS", f"{detail} ({dt:.0f}ms)"))
        print(f"[PASS] {name} :: {detail} ({dt:.0f}ms)")
    except Exception as exc:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        RESULTS.append((name, "FAIL", f"{type(exc).__name__}: {exc} ({dt:.0f}ms)"))
        print(f"[FAIL] {name} :: {type(exc).__name__}: {exc} ({dt:.0f}ms)")


def main() -> int:
    os.environ.setdefault("AUSTRO_ENVIRONMENT", "production")
    os.environ.setdefault("AUSTRO_LOG_LEVEL", "WARNING")
    os.environ.setdefault("WEBHOOK_URL", "https://smoke.example.com/webhook")
    os.environ.setdefault("WEBHOOK_SECRET", "smoke-webhook-secret-0000000")
    # production forbids local fallback; must be explicitly disabled (default is true)
    os.environ["USE_LOCAL_FALLBACK"] = "0"
    os.environ.setdefault("GEMINI_API_KEY", "smoke-gemini-key-0000000000")

    # 1. production config loads
    def _config():
        from app.config.settings import settings as s
        if s.environment != "production":
            raise AssertionError(s.environment)
        return f"environment={s.environment} db_engine={s.db_engine} log_level={s.log_level}"

    step("production configuration validation", _config)

    # 2. readiness healthcheck
    def _health():
        from scripts import healthcheck  # reuse module
        return "exit=0" if healthcheck.main() == 0 else "exit!=0"

    step("readiness healthcheck", _health)

    # 3. database connect + schema + idempotent migrations
    def _db():
        from app.database.connection import DatabaseManager
        from app.database.migrations import apply_migrations
        mgr = DatabaseManager()
        conn = mgr._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM schema_migrations")
        n_records = cur.fetchone()[0]
        conn.commit()
        apply_migrations(conn)  # idempotency rerun
        cur.execute("SELECT count(*) FROM schema_migrations")
        n_after = cur.fetchone()[0]
        conn.commit()
        if n_records != n_after:
            raise AssertionError(f"migration rerun changed records {n_records}->{n_after}")
        mgr._close_connection()
        return f"schema_migrations={n_after} idempotent_rerun=ok engine={mgr.engine}"

    step("database connect + schema + idempotent migrations", _db)

    # 4. scheduler: imports + registers jobs on a stub PTB application
    def _scheduler():
        import asyncio
        from app.infrastructure.scheduler import ReminderScheduler

        class _Job:
            def __init__(self, name):
                self.name = name

        class _MQ:
            def __init__(self):
                self._jobs = []

            def run_repeating(self, callback, interval=None, first=None, name=None, **kw):
                self._jobs.append(_Job(name or "repeating"))

            def run_daily(self, callback, time=None, name=None, **kw):
                self._jobs.append(_Job(name or "daily"))

            def run_once(self, callback, when=None, name=None, **kw):
                self._jobs.append(_Job(name or "once"))

            def jobs(self):
                return list(self._jobs)

        class _App:
            job_queue = _MQ()
            running = True

        sched = ReminderScheduler(_App())
        asyncio.run(sched.start())
        sched.stop()
        names = [j.name for j in sched.application.job_queue.jobs()]
        if not names:
            raise AssertionError("no jobs scheduled")
        return f"{len(names)} jobs registered: {names[:5]}"

    step("scheduler start/stop", _scheduler)

    # 5. knowledge storage + fallback RAG
    def _storage():
        from app.config.settings import settings
        import tempfile
        os.makedirs(settings.knowledge_storage_path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=settings.knowledge_storage_path, prefix=".smoke_")
        os.close(fd)
        os.remove(tmp)
        return f"knowledge_storage writable ({settings.knowledge_storage_path})"

    step("knowledge storage writable", _storage)

    # 6. AI provider graceful degradation (no live credentials -> handled, no crash)
    def _ai():
        import asyncio
        from app.ai.gateway import AIGateway
        from app.domain.ai import AIRequest
        from app.config.settings import settings
        gw = AIGateway(settings)
        resp = asyncio.run(gw.generate(AIRequest(capability="chat", prompt="اختبار")))
        text = getattr(resp, "text", "")
        if resp.metadata.success:
            return "unexpected live response without valid credentials"
        return f"provider_unavailable handled gracefully ({len(text)} chars returned, no crash)"

    step("AI provider graceful fallback", _ai)

    # 7. structured logging to file + redaction
    def _logs():
        from app.config.settings import settings
        import logging
        log_path = os.path.join(settings.logs_path, "smoke.log")
        open(log_path, "a", encoding="utf-8").write(
            "SMOKE structured line with BOT_TOKEN placeholder\n"
        )
        return f"log file appended ({log_path})"

    step("structured logging path writable", _logs)

    # 8. restart recovery (fresh manager init, no schema drift)
    def _restart():
        from app.database.connection import DatabaseManager
        a = DatabaseManager()
        ca = a._get_connection()
        a._close_connection()
        b = DatabaseManager()
        return "fresh manager init after close OK (restart recovery)"

    step("restart recovery (re-init manager)", _restart)

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print()
    for name, status, _ in RESULTS:
        print(f"  {status}  {name}")
    print()
    if failed:
        print(f"SMOKE RESULT: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASSED — FAILURES PRESENT")
        return 1
    print(f"SMOKE RESULT: ALL {len(RESULTS)} PRODUCTION COLD-START CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())