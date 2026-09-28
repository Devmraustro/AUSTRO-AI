# AUSTRO AI — Release Checklist

Ordered, actionable steps to take this codebase from the current
**PHASE G — ENGINEERING COMPLETE / LAUNCH BLOCKED** state to a live launch.
Each step lists the command or file to use and what "done" means.

---

## MUST DO (engineering gates)

- [x] **1. Port the SQLite-native repository SQL layer to PostgreSQL.**
   Completed via `app/database/dialect.py` (PolyglotCursor / CompatRow /
   `translate_query`, OR/REPLACE/Ignored registries, GREATEST, lastrowid,
   `DB_ERROR`) + per-test engine patching in `tests/test_repositories_pg.py`
   (18/18 on real PG). Smoke on PG: 8/8 PASSED.

- [x] **2. Re-run the full regression suite.**
   `python -m pytest tests -q --basetemp "$env:TEMP\pytest-run"` (allow ~40 min on
   this machine; each DB test initializes a fresh SQLite file). Target: no
   failures. → **183 passed, 0 failed** (default env; PG tests self-hosted via
   per-test fixture). `PRODUCTION_E2E_CHECKLIST.md` §1.7 updated to VERIFIED.

## REQUIRES LIVE CREDENTIALS (operator)

- [ ] **3. Obtain a real Telegram `BOT_TOKEN`** from @BotFather.
- [ ] **4. Obtain a real `GEMINI_API_KEY`** for the Google Gemini project.
- [ ] **5. Run the live Telegram E2E** exactly per `TELEGRAM_E2E_PROCEDURE.md`
  (onboarding → goals → habits → plans → study → knowledge upload + RAG Q&A →
  memory → coaching → reminders → export/delete → error/rate-limit UX).
  Mark `PRODUCTION_E2E_CHECKLIST.md` §5 items VERIFIED.

## PROVISION (operator / DevOps)

- [ ] **6. Provision externally managed PostgreSQL** (managed service or dedicated
  host). Create role + database; grant privileges. Record `DB_HOST/PORT/NAME/USER/
  PASSWORD`. DB sizing: 44 tables, expected modest RPS; set WAL, `password_encryption`
  sane defaults. Local-only alternative: `docker compose --profile local-infra up -d postgres`.

- [ ] **7. Deploy the external monitoring stack** (UptimeRobot + Prometheus/
  Grafana/Alertmanager per `MONITORING.md`): heartbeat on the webhook/health
  endpoint, DB connection alerts, AI provider alerts, backup-failed alert, disk/
  memory thresholds. Wire alertmanager to the ops Slack/Telegram.

- [ ] **8. Wire rate-limit guards into handler entry points.**
  `app/security/rate_limiter.py` exposes `check_command_rate_limit`,
  `check_ai_rate_limit`, `check_upload_rate_limit`, `check_ingestion_rate_limit`,
  `check_expensive_operation_limit`, `check_global_ai_limit`, and
  `RateLimitMiddleware`. Add the guard + `RateLimitError` → friendly reply at each
  public conversation entry before the state machines (recommend: top of
  `app/telegram/handlers.py` command/state entry functions and
  `app/telegram/main.py` message router).

## HARDENING (after E2E passes)

- [ ] **9. Fix the misleading "Using local fallback." warning** in
  `app/ai/gemini.py` (lines 83–92) so provider errors don't imply fallback when
  `USE_LOCAL_FALLBACK=0`.
- [ ] **10. Configure daily backup cron** (02:00 UTC) + retention per
  `DISASTER_RECOVERY.md`; run `scripts/pg_backup.py` nightly and one restore
  drill per release.
  - ✅ **Code side done (2026-09-27):** the schedule is now an independent
    `backup` Compose service (`Dockerfile.backup` + `scripts/backup_scheduler.py`
    → one-shot `scripts/pg_backup.py`), daily 02:00 UTC, 7 daily + 30 monthly
    retention, `PGPASSFILE` auth, lock + missed-run recovery, 31 tests in
    `tests/test_backup.py`. See `DISASTER_RECOVERY.md` §2.
  - ⬜ **Still open (needs the host):** start the service in production
    (`docker compose --env-file .env up -d app backup`) and observe one real
    `6/6: SUCCESS` at 02:00 UTC (`DISASTER_RECOVERY.md` §2.4), then run the
    destructive restore drill against a disposable DB once per release. Until
    then item 10 is **not operationally complete**.
- [x] **11. Release a dry run on a staging slot first** — code ready, live deploy
  still open:
  - ✅ `docker-compose.staging.yml`: standalone, `name: austro-staging`, staging
    containers/network/volumes, `TELEGRAM_TRANSPORT=polling`,
    `USE_LOCAL_FALLBACK=false`, staging-marked DB required, staging-only
    profile-gated backup worker and local PostgreSQL 16.
  - ✅ `.env.staging.example` (committed, no secrets) + `.env.staging`
    (git-ignored); `scripts/staging_validate.py` refuses any credential shared
    with production, without printing values.
  - ✅ Fail-closed staging rules in `app/config/deployment_validation.py`
    (dedicated staging DB, credentials, staging id, supported transport,
    non-placeholder token). Production's rules are unchanged.
  - ✅ `scripts/staging_validate.py`: 10-step plan/execute harness (compose,
    isolation, build, cold start + migrations, healthcheck, smoke, Telegram
    startup, DB isolation, persistent storage/logs, restart recovery).
  - ✅ `scripts/smoke_test.py` reusable for staging (production default
    unchanged).
  - ✅ `scripts/staging_rollback.py`: staging-only, identifiable verified image,
    health-gated, forward-only migrations, restore refused.
  - ✅ `tests/test_staging.py` (66 tests) + CI job validating both compose files,
    building both images and proving the isolation/rollback guards.
  - ✅ Procedure: `STAGING_RUNBOOK.md`.
  - ✅ CI builds the real images, which exposed three latent build blockers in
    the shared `Dockerfile`/`Dockerfile.backup` (missing `postgresql-client` for
    `pg_isready`; a `chmod` that ran as the non-root user; an unresolvable PGDG
    apt dependency in the backup image). All three fixed — see
    `STAGING_RUNBOOK.md` §7.
  - ⬜ **Still open (needs the host):** provision a staging PostgreSQL, a second
    @BotFather bot and a staging webhook URL/secret, then run
    `python scripts/staging_validate.py --execute` and record the 10 results.
    Docker, the staging database and the bot are not available on this host, so
    no live staging deployment has happened yet — item 11 is **not operationally
    complete**.

## GO-LIVE (production)

- [ ] **12. Set `.env`** per `PRODUCTION_RUNBOOK.md` §2 — remember
  `USE_LOCAL_FALLBACK=0` and `AUSTRO_LOG_LEVEL=WARNING|ERROR`, real secrets.
- [ ] **13. Deploy**: `docker compose --env-file .env up -d app`; confirm
  container HEALTHY, first bootstrapped schema, log file rotating, backups
  scheduled.
- [ ] **14. Post-launch 24h monitoring review** against `MONITORING.md` alert
  table; confirm no `visibility` violations in redaction.

---

## G.2 — Production Deployment & Live Launch Gate (executed)

| Gate | Result | Evidence |
|------|--------|----------|
| Production target (Arch B) | ✅ DEFINED | PG 16.6, `austro_ai` DB, role `austro` |
| Secrets posture | ✅ CLEAN | No hardcoded secrets; no `.env` committed; `${VAR}` interpolation |
| Production DB | ✅ VERIFIED | Connectivity, migrations, 44 tables, app query OK |
| Persistent storage | ✅ VERIFIED | `knowledge_storage` writable |
| Deployment mechanism | ⛔ BLOCKED | Docker CLI unavailable; no production host/provider |
| Startup / readiness | ✅ VERIFIED | `healthcheck.py` → HEALTHY (exit 0) |
| Telegram live E2E | ⛔ BLOCKED | No real `BOT_TOKEN` |
| Monitoring | ⛔ BLOCKED | External service unavailable |
| Backup | ✅ VERIFIED | 44 tables, 1211 rows, dump + gzip+sha256 OK |
| Restore drill | ✅ VERIFIED | sha256 match, 44 tables/1211 rows, app reconnect OK |
| Restart / recovery | ✅ VERIFIED | init→healthcheck→restart→healthcheck, all HEALTHY |
| Rollback | ⛔ BLOCKED | Requires isolated staging target |
| Security final | ✅ CLEAN | No secrets, no admin/debug endpoints, prod env enforced |
| Real-world smoke | ✅ VERIFIED | `smoke_test.py` — 8/8 PASSED |

**Overall: PHASE G.2 — ENGINEERING READY / LAUNCH BLOCKED** (blocked by
external deployment infrastructure + live Telegram credentials).

---

## Definition of done for launch

- [x] Full suite green on the chosen production DB engine (PostgreSQL after step 1).
  → **183 passed, 0 failed** (`python -m pytest tests -q`).
- [ ] Live Telegram E2E scenarios 5.1–5.8 all VERIFIED (requires real `BOT_TOKEN`).
- [ ] Monitoring alerts firing/acknowledged, backup + restore drill proven on the
  production instance.
- [ ] `PHASE_G_PRODUCTION_REPORT.md` re-issued with **B — PRODUCTION LAUNCH VERIFIED COMPLETE** (blocked on live E2E).
- [x] G.1 data-access dual-engine port verified: smoke 8/8 on PG, full suite green, pyflakes/compileall clean.
- [x] G.2 application-level gates executed: readiness, storage, DB, backup/restore, restart, security, smoke.
- [ ] G.2 deployment mechanism executed: BLOCKED — Docker CLI unavailable; external host/provider required.
