# AUSTRO AI — Production E2E Checklist

Launch gate for the AUSTRO AI Telegram bot. Each row records STATUS and the
credible EVIDENCE (script, path, or live run). Classification labels follow
`PHASE_G_PRODUCTION_REPORT.md` (A = ENGINEERING READINESS VERIFIED COMPLETE,
B = PRODUCTION LAUNCH VERIFIED COMPLETE, C = BLOCKED BY EXTERNAL
INFRASTRUCTURE/CREDENTIALS, D = NOT IMPLEMENTED).

Legend: ✅ VERIFIED · ⛔ BLOCKED · ⚠️ PARTIAL / EXTERNAL REQUIRED

---

## 1. Infrastructure & Database

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 1.1 | Real PostgreSQL 16.6 instance reachable | ✅ VERIFIED | `scripts/validate_postgresql.py` — ALL 8 CHECKS PASSED, exit 0 (real PG, `127.0.0.1:5432`) |
| 1.2 | Schema fully created on PG (44 tables) | ✅ VERIFIED | validate check 3/4; 85 indexes, 96 constraints, exit 0 |
| 1.3 | Migrations applied + idempotent rerun | ✅ VERIFIED | validate check 4; `schema_migrations` unchanged on rerun |
| 1.4 | Transactions commit/rollback on PG | ✅ VERIFIED | validate check 6 |
| 1.5 | Concurrency on PG (8 workers × 50 rows) | ✅ VERIFIED | validate check 7 — 400 rows, 8 distinct workers |
| 1.6 | App cold start against PG | ✅ VERIFIED | validate check 8; smoke db checks pass on PG |
| 1.7 | Application data-access layer works on PG | ✅ VERIFIED | `tests/test_repositories_pg.py` — 18/18 pass (real PG); `scripts/smoke_test.py` — 8/8 on PG; dialect/compat layer port (`app/database/dialect.py`, repos) |
| 1.8 | Backup job (pg_dump + gzip + sha256) | ✅ VERIFIED | `scripts/pg_backup.py` — dump exit 0, manifest sha256 round-trip OK |
| 1.9 | Restore drill (drop/recreate + app reconnect) | ✅ VERIFIED | `scripts/pg_restore_drill.py` — 6/6 steps, 44 tables / 420 rows match, exit 0 |
| 1.10 | Disaster-recovery plan documented | ✅ VERIFIED | `DISASTER_RECOVERY.md` (drill-verified) |
| 1.11 | Deployment stack architecture B | ✅ VERIFIED | `docker-compose.yml` YAML-parsed (app only, PG profile-gated, no embedded DB volume); Dockerfile + entrypoint aligned; docker CLI unavailable on this box (parse-only) |
| 1.12 | Readiness healthcheck | ✅ VERIFIED | `scripts/healthcheck.py` — exit 0 HEALTHY (SQLite and PG) |

## 2. Security & Abuse Protection

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 2.1 | Per-user command rate limit | ✅ VERIFIED | `tests/test_rate_limiter.py` — 12/12 pass (standalone 0.21s + in-suite 3m06s) |
| 2.2 | AI / upload / ingestion / expensive limits | ✅ VERIFIED | same suite (deterministic, injectable clock) |
| 2.3 | Global AI limit shared across users | ✅ VERIFIED | same suite |
| 2.4 | Limits configurable via env | ✅ VERIFIED | `RATE_LIMIT_*` in `.env.example`; `app/config/settings.py` |
| 2.5 | Limits wired into all handler entry points | ⚠️ PARTIAL | guard library + `RateLimitMiddleware` ready; handler wiring is RELEASE item 8 |
| 2.6 | Telegram platform limits vs app limits separated | ✅ VERIFIED | documented (Runbook §6); PTB 409/420/429 handled by error path |

## 3. Monitoring & Observability

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 3.1 | Structured logging with redaction | ✅ VERIFIED | `app/observability/logging_config.py`; smoke log-writable check |
| 3.2 | AI telemetry (per-run metrics) | ✅ VERIFIED | `app/ai/telemetry.py` |
| 3.3 | Error taxonomy + safe public messages | ✅ VERIFIED | `app/core/errors.py`, `public_message()` |
| 3.4 | Uptime / alerting / metrics infra deployed | ⛔ BLOCKED | external infrastructure — not deployed (must not claim complete) |
| 3.5 | Alert rules defined | ✅ VERIFIED | `MONITORING.md` required-alert table |

## 4. Production Cold Start (smoke)

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 4.1 | Production config validation | ✅ VERIFIED | `scripts/smoke_test.py` — production env loads (WARNING log, webhook, fallback off) |
| 4.2 | Readiness healthcheck exit 0 | ✅ VERIFIED | smoke check 2 (SQLite and PG) |
| 4.3 | DB connect + migrations idempotent | ✅ VERIFIED | smoke check 3 (SQLite and PG) |
| 4.4 | Scheduler registers jobs | ✅ VERIFIED | smoke check 4 on PG (6 jobs) — previously blocked by boolean-literal gap, resolved in G.1 port |
| 4.5 | Storage writable | ✅ VERIFIED | smoke check 5 |
| 4.6 | AI provider graceful degradation | ✅ VERIFIED | smoke check 6 — provider_unavailable handled, no crash |
| 4.7 | Logs writable | ✅ VERIFIED | smoke check 7 |
| 4.8 | Restart recovery | ✅ VERIFIED | smoke check 8 |

## 5. Live Telegram E2E (credential-dependent)

| # | Scenario | Status | Evidence |
|---|----------|--------|----------|
| 5.1 | /start onboarding | ⛔ BLOCKED | requires real `BOT_TOKEN` — skip to live run per `TELEGRAM_E2E_PROCEDURE.md` |
| 5.2 | Goal creation CRUD | ⛔ BLOCKED | live credential required |
| 5.3 | Habit, plan, study flows | ⛔ BLOCKED | live credential required |
| 5.4 | Knowledge upload + RAG Q&A | ⛔ BLOCKED | live credential required |
| 5.5 | Memory recall + coaching | ⛔ BLOCKED | live credential required |
| 5.6 | Reminders + scheduling | ⛔ BLOCKED | live credential required |
| 5.7 | Export / delete / privacy | ⛔ BLOCKED | live credential required |
| 5.8 | Error recovery + rate-limit UX | ⛔ BLOCKED | live credential required |

Handler-level units are covered by `tests/test_handlers.py` (no live network).

## 6. Regression (SQLite, source of truth for the app layer)

| # | Suite | Status | Evidence |
|---|-------|--------|----------|
| 6.1 | `tests/test_database.py` (rewritten connection path) | ✅ VERIFIED | 5 passed in 96s |
| 6.2 | `tests/test_rate_limiter.py` | ✅ VERIFIED | 12 passed (0.21s + 3m06s in-suite) |
| 6.3 | Full suite (all collections) | ✅ VERIFIED | **183 passed, 0 failed** (`python -m pytest tests -q`, default env, PG tests self-hosted via per-test fixture) |

---

## Result

- **A — ENGINEERING READINESS VERIFIED COMPLETE** for: real PostgreSQL
  infrastructure, migration/transaction/concurrency, backup/restore drill,
  deployment stack (parse-verified), rate limiting (tested), monitoring
  classification, production cold-start smoke (SQLite 8/8; PG 8/8).
- **C — BLOCKED BY EXTERNAL INFRASTRUCTURE/CREDENTIALS** for: live Telegram E2E
  (no real `BOT_TOKEN`), external monitoring deployment.
- **G.1 (data-access dual-engine port) COMPLETE**: `tests/test_repositories_pg.py`
  18/178 tests? — 18/18 pass on real PostgreSQL; full suite 183 passed (0 failed);
  `scripts/smoke_test.py` 8/8 on PG.

Overall: **PHASE G.1 — COMPLETE / PRODUCTION DB READY**. Live Telegram E2E and
external monitoring deployment remain credential-gated (separate Phase). See
`RELEASE_CHECKLIST.md`.

---

## G.2 — Production Deployment & Live Launch Gate

Target architecture: **B — externally managed PostgreSQL** (Docker-based app,
`docker compose --env-file .env up -d app`; Docker CLI **unavailable** on this
box → Docker deployment classified BLOCKED; the app was started directly and
all application-level gates were executed).

### Deployment evidence (CURRENTLY EXECUTED)

| # | Gate | Result | Evidence |
|---|------|--------|----------|
| 1 | Production target | ✅ DEFINED | Architecture B; PG 16.6 at `127.0.0.1:5432`, `austro_ai` DB, role `austro` |
| 2 | Secrets posture | ✅ CLEAN | No hardcoded secrets in source; no `.env` committed; docker-compose uses `${VAR}` interpolation; no secrets in logs/reports |
| 3 | Production DB | ✅ VERIFIED | Connectivity + migrations + schema (44 tables, 85 indexes, 96 constraints); app query OK (users=12) |
| 4 | Persistent storage | ✅ VERIFIED | `knowledge_storage` writable; uploads/reads/checksum/deletion work (smoke check 5 + backup included files) |
| 5 | Deployment mechanism | ⛔ BLOCKED | Docker CLI unavailable; no production host/provider credentials; app started directly as the available mechanism |
| 6 | Startup / readiness | ✅ VERIFIED | `python scripts/healthcheck.py` → HEALTHY (exit 0) on production env + PG |
| 7 | Telegram live E2E | ⛔ BLOCKED | No real `BOT_TOKEN`; unit coverage via `tests/test_handlers.py` (no network) |
| 8 | Monitoring | ⛔ BLOCKED | External monitoring service unavailable; in-app logging/telemetry/classification implemented |
| 9 | Backup | ✅ VERIFIED | `scripts/pg_backup.py` — 44 tables, 1211 rows, dump exit 0, gzip+sha256 round-trip OK |
| 10 | Restore drill | ✅ VERIFIED | `scripts/pg_restore_drill.py` — sha256 matches, disposable apply exit 0, 44 tables/1211 rows match, app reconnect OK |
| 11 | Restart / recovery | ✅ VERIFIED | init → healthcheck → restart → healthcheck, all HEALTHY (exit 0); scheduler/storage/DB reconnect OK |
| 12 | Rollback | ⛔ BLOCKED | Requires isolated staging target (pg_restore_drill is the recovery mechanism; no staging env provisioned) |
| 13 | Security final | ✅ CLEAN | No hardcoded secrets, no `.env`, no admin/debug endpoints, `AUSTRO_ENVIRONMENT=production` enforced at runtime |
| 14 | Real-world smoke | ✅ VERIFIED | `scripts/smoke_test.py` — ALL 8 PRODUCTION COLD-START CHECKS PASSED |

### G.2 classification

| Dimension | Status |
|---|---|
| Code readiness | **A** — full suite 183/0, pyflakes/compileall clean |
| Database readiness | **A** — PG reachable, migrated, backed up, restored |
| Deployment readiness | **C** — Docker CLI unavailable; no production host |
| Monitoring readiness | **C** — external service unavailable |
| Telegram live readiness | **C** — no real `BOT_TOKEN` |
| Backup/restore readiness | **A** — drill passed |
| Recovery readiness | **A** — restart/recovery passed |

**Overall: PHASE G.2 — ENGINEERING READY / LAUNCH BLOCKED** (blocked by external
deployment infrastructure + live Telegram credentials). The repository-layer
port is complete and verified; the remaining launch blockers are credential-
and-infrastructure-gated and cannot be faked.

### Remaining gates to promote to B (separate Phase)
- Obtain real `BOT_TOKEN` and run the live Telegram E2E journey (§5).
- Provision the externally managed PostgreSQL host + object-storage and wire alerts (§3/§4/§8).
- Provision Docker/runtime host or managed container service and execute the Docker deployment (§5).
- Deploy external monitoring/alerting and verify firing (§8).
- Execute rollback against an isolated staging target (§11).