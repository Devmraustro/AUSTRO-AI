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
DB_SSLMODE=verify-full     # REQUIRED for a non-loopback host
DB_SSL_ROOT_CERT=/etc/ssl/certs/postgres-ca.crt
```

Failure to set `USE_LOCAL_FALLBACK=0` (its default is true) will make the app
REFUSE to start in production. See `.env.example` for the full variable list,
including the `RATE_LIMIT_*` and `KNOWLEDGE_*` tunables.

### PostgreSQL transport encryption

`DB_SSLMODE` must be `require` or `verify-full` for any host that is not
loopback or the compose service name `db`. The application **refuses to
connect** otherwise: libpq's own default is `prefer`, which attempts TLS and
then silently falls back to plaintext, sending `DB_PASSWORD` and every memory
and knowledge row unencrypted with no error and no log line. Prefer
`verify-full` with `DB_SSL_ROOT_CERT` pointing at your CA bundle. The only
escape hatch is `DB_ALLOW_INSECURE=1`, which is never a default and must not be
set in production.

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
`DB_ENGINE=postgresql`, bounded by `DB_WAIT_TIMEOUT_SECONDS`, default 120s, then
fails fast with a clear message) and runs `DatabaseManager()` +
`apply_migrations()` before booting the bot, so schema is applied before first
traffic. Three build/start blockers were fixed in the same change: the app image
now ships `postgresql-client` (so `pg_isready` exists); the entrypoint is
copied with `COPY --chmod=0755` before the `USER austro` switch (a `RUN chmod`
there fails with "Operation not permitted" and made the image unbuildable); and
`Dockerfile.backup` is now based on the official `postgres:16-bookworm` image
instead of installing `postgresql-client-16` from PGDG, which could not resolve
on bookworm and made the backup image unbuildable. CI now builds both images and
asserts `pg_dump (PostgreSQL) 16.x` in the backup image. See `STAGING_RUNBOOK.md` §7.

### Staging is a separate stack, never this one

Dry runs happen in a completely separate stack: `docker-compose.staging.yml`
(project `austro-staging`, its own containers, network and volumes, a dedicated
staging database, a second @BotFather bot and separate webhook settings). Every
command in this section uses `docker-compose.yml` implicitly and must never be
run against the staging file or vice versa. See `STAGING_RUNBOOK.md`.

## 4. Backup & restore

See `DISASTER_RECOVERY.md` (drill-verified on real PostgreSQL):

- **Automated daily backup (independent container):** the `backup` Compose
  service runs `scripts/backup_scheduler.py`, which triggers the one-shot
  `scripts/pg_backup.py` every day at **02:00 UTC** and writes into the shared
  `backups` volume. It is never started inside the Telegram update loop.
  ```bash
  docker compose --env-file .env up -d app backup
  docker compose logs -f backup          # expect "6/6: SUCCESS backup=..." ~02:00 UTC
  docker compose run --rm backup python scripts/pg_backup.py /app/backups   # manual
  ```
  Missed runs are recovered on start-up (if today's slot passed with no backup
  for today, it runs immediately); a lock file prevents duplicate concurrent
  runs, and a failed day is retried on a bounded backoff
  (`BACKUP_RETRY_MINUTES`, default 60) rather than in a hot loop.
  `Dockerfile.backup` pins `postgresql-client-16` so `pg_dump` matches the
  production PostgreSQL 16 server, and credentials are passed via `PGPASSFILE`
  so the password never appears in argv or logs.
- Retention: newest 7 backups + the newest backup of each UTC month for up to
  30 further months, applied **only after** the new archive/manifest pair is
  published and verified. Details: `DISASTER_RECOVERY.md` §2.3.
- Verify integrity + restore drill (destructive, disposable DB only):
  `python scripts/pg_restore_drill.py <archive> <manifest>`.
- Restore to a production-like target uses `pg_dump --clean --if-exists
  --no-owner --no-acl` piped into `psql`, then the app reconnect path.
- RPO 24h, RTO <= 30 min. Status: automation implemented and unit-tested, but
  **not yet installed on the production host** — do not mark the nightly
  backup as active until `docker compose logs backup` shows a real 02:00 UTC
  success (see release checklist item 10).

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