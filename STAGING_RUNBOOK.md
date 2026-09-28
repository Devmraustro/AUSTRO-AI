# AUSTRO AI — Staging Runbook

Release-checklist item 11: run a full dry run on an **isolated staging slot**
before anything is deployed to production.

This runbook is the procedure. It is safe to follow exactly: nothing in it can
start, stop, reconfigure or restore production.

---

## 1. What "isolated" means here

| Concern | Production | Staging |
|---------|-----------|---------|
| Compose file | `docker-compose.yml` | `docker-compose.staging.yml` (standalone, **not** an override) |
| Project name | `austro` | `austro-staging` |
| Containers | `austro-ai-app`, `austro-ai-backup` | `austro-staging-app`, `austro-staging-backup`, `austro-staging-db` |
| Network | `austro-network` | `austro-staging-network` |
| Volumes | `austro_{knowledge_storage,logs,backups,postgres_data}` | `austro-staging_{…}` (separate names) |
| Host port | `8443` | `9443`, bound to `127.0.0.1` by default |
| Database | externally managed, `austro_ai` | **dedicated**, name must contain a staging marker (e.g. `austro_ai_staging`) |
| Bot token | production bot | **second bot** from @BotFather |
| Webhook URL + secret | production | **different** values |
| `AUSTRO_ENVIRONMENT` | `production` | `staging` |
| `USE_LOCAL_FALLBACK` | `false` | `false` (production parity) |
| `AUSTRO_LOG_LEVEL` | `WARNING`/`ERROR` | `INFO`/`DEBUG` (the one intentional difference) |

Both stacks may share a host **only** when all of the above are true. Sharing a
host is allowed; sharing a database, a bot token, a network or a volume is not.

---

## 2. One-time setup

```bash
cp .env.staging.example .env.staging     # then fill in the real values
```

Required in `.env.staging` (the stack refuses to start without them, and
`app/config/deployment_validation.py` refuses to run without them):

| Key | Requirement |
|-----|-------------|
| `AUSTRO_ENVIRONMENT` | `staging` |
| `STAGING_ID` | non-empty, e.g. `staging-1` |
| `BOT_TOKEN` | the **second** bot's token from @BotFather |
| `GEMINI_API_KEY` | a staging key (parity with production) |
| `WEBHOOK_URL` / `WEBHOOK_SECRET` | staging values, secret ≥ 16 characters, different from production |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | a **dedicated staging** database; `DB_NAME` must contain `staging`/`uat`/`sbx`/`preprod` |

`.env.staging` is git-ignored. Only `.env.staging.example` (no secrets) is
committed. Never commit the real file.

### Fail-closed rules (enforced in code, tested in CI)

At startup, staging additionally requires:

* `DB_ENGINE=postgresql`
* `USE_LOCAL_FALLBACK=false` and a non-empty `GEMINI_API_KEY`
* a non-placeholder bot token (CI/demo tokens are rejected)
* `TELEGRAM_TRANSPORT=polling` (the only implemented transport)
* a dedicated, staging-marked database name — `austro_ai` is rejected

Production's own five rules are unchanged by any of this.

### Telegram transport: what is real today

`app/telegram/main.py` **long-polls**. `WEBHOOK_URL`, `WEBHOOK_SECRET` and
`WEBHOOK_PORT` are validated configuration (and must be unique staging values),
but the runtime does not serve a webhook and does not need a public port. Setting
`TELEGRAM_TRANSPORT=webhook` fails at startup rather than silently polling.
Confirming the staging bot really answers Telegram is a human step in
`TELEGRAM_E2E_PROCEDURE.md` §"Staging slot".

---

## 3. Deploy

```bash
D="docker compose -f docker-compose.staging.yml -p austro-staging --env-file .env.staging"

$D config -q            # resolves; fails fast on a missing key
$D up -d --build app    # starts the staging app only
$D ps                   # app must reach `healthy`
$D logs --tail 50 app
```

`docker-entrypoint.sh` waits for PostgreSQL (bounded by
`DB_WAIT_TIMEOUT_SECONDS`, default 120s) and then applies startup migrations
idempotently. A wrong `DB_HOST` therefore fails with a clear message instead of
restart-looping forever.

`$D down` stops **staging only**. It never touches `docker-compose.yml`.

### Optional services (profile-gated, never started by accident)

```bash
$D --profile staging-backup up -d backup     # staging-only backup worker
$D --profile local-staging-db up -d staging-db   # local PostgreSQL 16, dry runs only
```

The `staging-db` profile creates a dedicated staging database and role on the
staging host. It is not, and cannot be, the production database.

---

## 4. Validate (automated)

```bash
python scripts/staging_validate.py                      # print the plan (runs nothing)
python scripts/staging_validate.py --static             # config isolation only, no Docker
python scripts/staging_validate.py --execute            # the full battery (needs Docker)
python scripts/staging_validate.py --execute --json     # machine-readable results
```

The ten checks, in order:

| # | Check | What it proves |
|---|-------|----------------|
| 1 | `compose_config` | the staging and production compose files both resolve |
| 2 | `config_isolation` | staging secrets differ from production and the DB is staging-marked (no values printed) |
| 3 | `image_build` | the staging app image builds |
| 4 | `cold_start` | the container starts and the entrypoint applies migrations |
| 5 | `healthcheck` | `scripts/healthcheck.py` prints HEALTHY, exit 0 |
| 6 | `smoke_test` | the full offline battery passes in staging mode |
| 7 | `telegram_startup` | supported transport, dedicated non-placeholder token (offline: the token is never put in a URL or log) |
| 8 | `database_isolation` | the container is bound to the staging DB, not `austro_ai` |
| 9 | `storage_logs` | the persistent storage and log volumes are writable |
| 10 | `restart_recovery` | a marker file and the schema survive `restart`, and the container returns to HEALTHY |

`PASS` / `FAIL` / `BLOCKED` are reported per check. **`BLOCKED` means "not
verified"** — it happens when Docker or the staging slot is unavailable and must
never be recorded as a successful deployment. The script exits non-zero on any
`FAIL`.

CI runs checks 1 and 2 plus both image builds; it has no staging slot, so
`BLOCKED` is expected there for the live steps.

---

## 5. Rollback

```bash
python scripts/staging_rollback.py --plan --image austro-staging-app:5f0d604
python scripts/staging_rollback.py      --image austro-staging-app:5f0d604
```

Guarantees, all enforced in code:

* **Staging only** — refuses unless `AUSTRO_ENVIRONMENT=staging` (read from
  `.env.staging`, not from your shell, so you cannot redirect it at production)
  and always uses `-f docker-compose.staging.yml -p austro-staging`.
* **Identifiable target** — the image must be an `austro-staging-app:<tag>`
  reference; `local`, `latest`, `main`, `dev` and `WIP-*` are rejected, and a
  short tag needs `--allow-unverified`.
* **Health gated** — the new container must become healthy and
  `scripts/healthcheck.py` must exit 0.
* **Migrations are forward-only** — there is no down migration. The script
  prints the applied `schema_migrations` rows so a human can decide whether an
  older image is safe. It never guesses and never reverses a migration.
* **Never restores** — `--allow-restore` is refused. Restore drills are run with
  `scripts/pg_restore_drill.py` against a **disposable staging database only**.

---

## 6. Known gaps (state these honestly)

* No live staging deployment has been executed: this host has no Docker CLI, no
  staging PostgreSQL, no staging bot and no webhook tier. Everything in §4 that
  needs Docker is **unverified**, not passing.
* Telegram webhook mode is not implemented; only configuration and polling
  startup are validated.
* `scripts/staging_validate.py --execute` has not been run end to end, so treat
  the live checks as untested code until the first staging deploy.
