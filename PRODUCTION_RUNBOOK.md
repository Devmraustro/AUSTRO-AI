# AUSTRO AI — Production Runbook

Operational guide for the AUSTRO AI Telegram bot. Pairs with `DISASTER_RECOVERY.md`,
`MONITORING.md`, `PRODUCTION_E2E_CHECKLIST.md`, `RELEASE_CHECKLIST.md` and the
`PHASE_G_PRODUCTION_REPORT.md` classification.

---

## 1. Architecture (selected: B — externally managed PostgreSQL)

- The app container connects to a **separately provisioned PostgreSQL** via
  `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`. There is no application-embedded
  production database volume.
- `docker-compose.yml` defines only the `app` service for production. The
  `postgres` service is gated behind `profiles: ["local-infra"]` and is for
  LOCAL development / DR drills only:
  `docker compose --profile local-infra up -d postgres`.
- Production run:
  `docker compose --env-file .env up -d app` (entrypoint waits for DB, applies
  migrations, then starts the bot).

## 2. Required environment (.env)

```
BOT_TOKEN=                # Telegram bot token (real, required)
GEMINI_API_KEY=           # Google Gemini key (required in production)
AUSTRO_ENVIRONMENT=production
AUSTRO_LOG_LEVEL=WARNING  # WARNING or ERROR required by the prod validator
WEBHOOK_URL=https://yourdomain.com/webhook
WEBHOOK_SECRET=           # strong random secret
USE_LOCAL_FALLBACK=0      # REQUIRED: prod validator rejects local fallback
DB_ENGINE=postgresql
DB_HOST=, DB_PORT=5432, DB_NAME=austro_ai, DB_USER=, DB_PASSWORD=
```

Failure to set `USE_LOCAL_FALLBACK=0` (its default is true) will make the app
REFUSE to start in production. See `.env.example` for the full variable list,
including the `RATE_LIMIT_*` and `KNOWLEDGE_*` tunables.

## 3. Lifecycle

| Action | Command |
|---|---|
| Start | `docker compose --env-file .env up -d app` |
| Stop | `docker compose down` |
| Restart | `docker compose restart app` |
| Logs | `docker compose logs -f app` |
| Readiness | `docker compose exec app python scripts/healthcheck.py` (exit 0 = HEALTHY) |
| Shell | `docker compose exec app sh` |

The Dockerfile `HEALTHCHECK` runs `scripts/healthcheck.py`; it verifies config +
BOT_TOKEN, DB reachability (`SELECT 1` on PostgreSQL / SQLite), and that
knowledge_storage + logs are writable.

`docker-entrypoint.sh` waits for the database (`pg_isready` when
`DB_ENGINE=postgresql`) and runs `DatabaseManager()` + `apply_migrations()` before
booting the bot, so schema is applied before first traffic.

## 4. Backup & restore

See `DISASTER_RECOVERY.md` (drill-verified on real PostgreSQL):

- Backup (logical, gzip level 6, SHA-256 manifest):
  `python scripts/pg_backup.py --engine postgresql`
  Outputs `backups/austro_ai_backup_<UTC>.sql.gz` + `.manifest.json`.
  Schedule daily 02:00 UTC; retention 7 days (30 monthly). RPO 24h, RTO ≤ 30 min.
- Verify integrity + restore drill:
  `python scripts/pg_restore_drill.py`.
- Restore to a production-like target uses `pg_dump --clean --if-exists
  --no-owner --no-acl` piped into `psql`, then the app reconnect path.

## 5. Monitoring & alerting

`MONITORING.md` classifies status honestly:

- **IMPLEMENTED (in-app):** JSON structured logs with secret redaction
  (`app/observability/logging_config.py`), AI telemetry (`app/ai/telemetry.py`),
  error taxonomy (`app/core/errors.py`), safe public error handling (`app/telegram/main.py`).
- **EXTERNAL REQUIRED (not deployed):** uptime, alerting, infra/DB metrics, log
  aggregation, distributed tracing. Recommended minimal stack: UptimeRobot +
  Prometheus/Grafana/Alertmanager (+ Loki optional). Required alerts are listed
  in `MONITORING.md` (app_down, db_connection_failed, ai_provider_unavailable,
  high_error_rate, high_latency, backup_failed, disk_space_low, memory_high).
- Do **not** claim "monitoring complete" until the external provider is deployed.

## 6. Application-level rate limiting

Per-user, deterministic, tested (`tests/test_rate_limiter.py`, 12/12 passing both
standalone and in-suite). Limits are configurable via env (defaults below):

| Bucket | Default | Period |
|---|---|---|
| command | 30 | 60s |
| ai_request | 20 | 60s |
| upload | 10 | 3600s |
| ingestion | 5 | 3600s |
| expensive | 10 | 3600s |
| global_ai (shared across users) | 100 | 60s |

- Implementation: `app/security/rate_limiter.py` (sliding-window token bucket),
  `RateLimitMiddleware` + async `check_*_rate_limit()` for handler integration.
- These are **application-level** per-user guards. Telegram platform limits
  (HTTP 409/420/429 "Too Many Requests", retry_after) are handled separately by
  python-telegram-bot's error path and must NOT be confused with app-level quotas.
- Before launch, wire the guards at the handler entry points listed in
  `RELEASE_CHECKLIST.md` (step 8).

## 7. Known gap: PostgreSQL data-access layer (IMPORTANT)

PostgreSQL infrastructure is fully validated (schema DDL, migrations,
transactions, concurrency, backup/restore, startup) — see
`scripts/validate_postgresql.py` (ALL 8 CHECKS PASSED). **However**, the
repository data-access layer is SQLite-native. A production smoke test on real
PostgreSQL caught the first live failure:

```
SELECT * FROM reminders WHERE is_active = 1   -- ERROR:
operator does not exist: boolean = integer
```

Confirmed SQLite-only constructs that must be ported before PostgreSQL can serve
live queries:

| File | Constructs |
|---|---|
| `app/database/repositories.py` | `?` bind params; `is_active = 1` (405, 419); `current_streak = 0` (287) |
| `app/knowledge/repositories.py` | `?` params; `verified = 1` (675) |
| `app/learning/repositories.py` | `?` params; `acknowledged = 1` (817); `datetime.utcnow().strftime(...)` timestamps (33); `cursor.rowcount >= 0` (536, 634) |
| `app/memory/repositories.py` | `?` params; `datetime.utcnow().strftime(...)` (38); `subject LIKE ?` (237, 245) |

Porting map (PostgreSQL dialect — see `app/database/dialect.py` precedent):

- `?` placeholder → `%s` (psycopg2 paramstyle) — 248 occurrences across the four
  repository modules.
- Boolean equality on `TRUE/FALSE` columns (`= 1`/`= 0`) → `= TRUE`/`= FALSE`.
- `datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")` → pass Python `datetime`,
  keep SQLite string format for write compatibility.
- `cursor.lastrowid` (INSERT id retrieval) → `INSERT ... RETURNING id` where used;
  retrofit the few `INSERT` sites relying on it.
- `cursor.rowcount` semantics are identical; keep guards ≥ 0.

Until this port is complete and the full suite passes against PostgreSQL, the
supported production database for live application queries is **SQLite**
(the default `DB_ENGINE`), with PostgreSQL validated at the infrastructure/DR
level only. Do not claim PostgreSQL production readiness for the app layer.

## 8. Known polish items

- `app/ai/gemini.py` logs "Using local fallback." on ANY provider error even when
  `USE_LOCAL_FALLBACK=0` (the gateway still honors the flag — the message is
  misleading). Re-word provider warning lines to avoid implying fallback occurs.

## 9. Incident response

- Structured errors are typed (`app/core/errors.py`); `public_message()` gives
  safe user text; `error_handler` in `app/telegram/main.py` logs full detail.
- See `SECURITY_INCIDENT_RESPONSE.md` and `PRIVACY_RETENTION.md` for the
  security/privacy response procedures.