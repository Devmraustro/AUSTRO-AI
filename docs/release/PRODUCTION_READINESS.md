# AUSTRO AI — Production Readiness

- **Date:** 2026-11-14
- **Current audited branch:** `main`
- **Current main SHA:** `85e9a53a04c7205b29c64722d3796ac322d9d6cb` (PR #18 merge commit)
- **Post-merge GitHub Actions:** [Run 43599696751](https://github.com/Devmraustro/AUSTRO-AI/actions/runs/43599696751) — 9/9 jobs passed (`push` event)
- **Earlier audit candidate:** `arena/8aa8f7e9-austro-ai` (historical; its PR #18 is now merged)
- **Verdict:** **NOT READY — NO-GO**
- **Production deployment performed:** **NO**
- **Production data/infrastructure/secrets changed:** **NO**

## 1. Verdict summary

The post-merge `main` includes the privacy, retention, PostgreSQL recovery, and learning hardening from PR #18. Its push-triggered CI run `43599696751` completed successfully with all 9/9 jobs passing, including the Python 3.10–3.14 matrix, dependency audit, performance tests, staging image/compose checks, and encrypted PostgreSQL 16 backup/restore. The earlier audit branch reported 1100 passed, 143 skipped, and 0 failed locally; that local result is historical evidence, not a fresh run on the current main SHA.

It is still **not production-ready** for these reasons:

1. Live Telegram end-to-end behavior was not exercised (no valid staging bot token or safe test chat).
2. Live OpenRouter behavior was not exercised (no real staging API key).
3. The staging runtime drill (`staging_validate.py --execute`) was not run; only compose resolution, static isolation, and image builds were verified in CI.
4. Production validation does not force PostgreSQL; a deploy must explicitly set `DB_ENGINE=postgresql` or the app will use SQLite.
5. The nullable-counter regression is now exercised through `LearningProgressRepository.record()` against a legacy-compatible nullable schema; the exact-head PR CI passed. This code-level regression does not replace the live staging, OpenRouter, and Telegram release gates below.

## 2. Readiness gates

| Gate | Status | Evidence |
|---|---|---|
| Audit findings fixed | **PASS (local)** | Four High, four Medium, two Low fixed + rollback/COALESCE hardening; 20 regression tests |
| Full SQLite suite | **PASS (Linux)** | `1100 passed, 143 skipped, 0 failed` on Linux (`arena/8aa8f7e9-austro-ai`) |
| PostgreSQL repository suite | **PASS** | 30 passed, 0 skipped, exit 0 (PR #17 CI run `43493434771`) |
| PostgreSQL claim suite | **PASS** | 7 passed, 0 skipped, exit 0 (PR #17 CI run `43493434771`) |
| Privacy/erasure regressions | **PASS** | 10 new tests passed; the 30 PostgreSQL repository tests passed separately |
| Learning/DB regressions | **PASS** | 10 passed (5 learning + 5 DB recovery) |
| Security test selection | **PASS** | 44 passed, 1 skipped (Docker image test skipped without Docker) |
| Lint / compile / SAST | **PASS** | pyflakes 0, compileall 0, Bandit 0 medium/high |
| Dependency audit | **PASS** | 33 dependencies, no known vulnerabilities (PR #17 CI run `43493434771`) |
| Evaluation harness | **PASS** | 100% / 100% threshold |
| Performance benchmarks | **PASS** | 7/7 |
| Staging compose + static isolation | **PASS** | compose resolves; 4 credentials differ from production |
| Staging images | **PASS (build)** | app and backup images built successfully (PR #17 CI run `43493434771`) |
| Staging runtime drill | **NOT RUN** | requires a running staging stack and credentials |
| Telegram live E2E | **BLOCKED** | no credentials/safe chat |
| OpenRouter live eval | **BLOCKED** | no API key; local fallback only |
| Secret scan (TruffleHog) | **PASS (PR candidate CI)** | passed in run `43531570063` on the audit candidate branch; this is not part of the post-merge push run |
| Backup/restore integration | **PASS (main push CI)** | encrypted backup and disposable PostgreSQL 16 restore passed in post-merge push run `43599696751` |
| CI matrix + post-merge gates (3.10–3.14) | **PASS (main)** | Push-triggered GitHub Actions run `43599696751` on `85e9a53a04c7205b29c64722d3796ac322d9d6cb`; all 9 jobs passed, including matrix, dependency audit, performance, staging compose/images, and encrypted PostgreSQL 16 backup/restore |
| PR #18 review/merge | **COMPLETE** | PR #18 merged; merge commit and `main` ref both equal `85e9a53a04c7205b29c64722d3796ac322d9d6cb` |
| Nullable-counter repository regression | **PASS (PR CI)** | The test invokes `LearningProgressRepository.record()` against a legacy-compatible nullable SQLite schema; exact-head CI run `43601276918` passed |

## 3. Configuration requirements for production

These are the settings a production host must provide. Every one is validated by `app/config/deployment_validation.py` unless marked otherwise.

| Setting | Required production value | Notes |
|---|---|---|
| `AUSTRO_ENVIRONMENT` | `production` | activates production validation |
| `AUSTRO_LOG_LEVEL` | `WARNING` or `ERROR` | production validation rejects INFO/DEBUG |
| `BOT_TOKEN` | real production bot token | placeholder markers are rejected |
| `OPENROUTER_API_KEY` | real key | required; local fallback must be off |
| `USE_LOCAL_FALLBACK` | `false` | production validation rejects `true` |
| `OPENROUTER_API_URL` | `https://...` | no embedded credentials |
| `OPENROUTER_MODEL` | valid `vendor/model` | no whitespace |
| `WEBHOOK_URL` | configured URL | validated, but the runtime currently long-polls |
| `WEBHOOK_SECRET` | configured secret | validated |
| `TELEGRAM_TRANSPORT` | `polling` | the only implemented transport; `webhook` is rejected |
| `DB_ENGINE` | **`postgresql`** | production validation does **not** force this; SQLite remains the default |
| `DB_NAME` | production database | `austro_ai` for production |
| `DB_USER` / `DB_PASSWORD` | dedicated credentials | no implicit login |
| `DB_SSLMODE` | `verify-full` recommended | remote hosts must be encrypted; `verify-full` also validates the certificate |
| `DB_SSL_ROOT_CERT` | CA bundle path | required for verified TLS |
| `ACTIVITY_LOG_RETENTION_DAYS` | `90` (or explicit policy) | `0` disables the purge |
| `COACH_LOG_RETENTION_DAYS` | `90` (or explicit policy) | `0` disables the purge |
| `KNOWLEDGE_STORAGE_PATH` | persistent volume | backups must include it |
| Backups | encrypted, off-host | key handling documented separately |

Additional operational requirements:

- Exactly **one** polling application instance, unless the scheduler is given a distributed lock. The reminder and retention jobs are in-process PTB job-queue jobs.
- Persistent storage must be mounted at the configured knowledge path; otherwise uploaded sources disappear on redeploy.
- The production database must be backed up before the first migration run, even though migrations are idempotent.
- Rotate the bot token and OpenRouter key if either was ever placed in a shell history, CI log, or `.env` file that left the host.

## 4. Feature status

| Feature | Status | Notes |
|---|---|---|
| Telegram commands and menus | **Verified offline** | handler registration and callback patterns tested; live E2E missing |
| AI chat | **Partial** | gateway/fallback/retry tested offline; live OpenRouter unverified |
| Privacy export `/export` | **Verified** | owner-scoped JSON, file delivery path tested |
| Account deletion `/delete` | **Verified** | token, expiry, replay/foreign refusal, transaction, verification, file handling |
| Retention purge | **Verified** | SQLite and PostgreSQL; timestamp-format bug fixed |
| Knowledge ingestion / RAG | **Verified offline + PostgreSQL** | extraction, traversal/ZIP guards, grounded-answer path |
| Learning / coaching | **Verified offline** | mastery, spaced review, assessments, plans; replay guard |
| XP system | **Not present** | no XP feature exists; mastery/progress counters are the actual model |
| Staging runtime | **Not executed** | compose/images only |
| Monitoring / alerting | **Documented, not runtime-verified** | see `MONITORING.md`; no production telemetry sink tested |
| Backup / restore | **CI-only** | encrypted backup job and disposable restore drill exist; not run locally |
| Webhook transport | **Not implemented** | polling only; config is validated but unused |

## 5. Required actions before production

1. Run the staging runtime drill with real staging credentials:
   `python scripts/staging_validate.py --env-file .env.staging --execute`
   and confirm cold start, migrations, healthcheck, DB identity, storage marker, restart.
2. Execute the live Telegram E2E procedure from `TELEGRAM_E2E_PROCEDURE.md` with a dedicated staging bot and test chat: `/start`, a goal flow, `/export`, `/delete` cancel/confirm, source deletion, reminder delivery.
3. Run at least one live OpenRouter generation and one provider-error/rate-limit check with a real key; confirm no secret is logged and telemetry records only metadata.
4. Run the encrypted backup and disposable restore drill against staging (or rely on the CI job with evidence attached to the release).
5. Set `DB_ENGINE=postgresql` explicitly and verify `DB_SSLMODE=verify-full` with the production CA bundle.
6. Prepare the rollback image/tag and confirm the healthcheck against the previous release.
7. Attach the CI run URL, staging drill output, and E2E evidence to the release record.

## 6. Stop conditions

Do not deploy if any of the following is true:

- CI is red, skipped, or the PostgreSQL tests skipped because the service was unreachable.
- Any pre-existing test was disabled or weakened to make the branch green.
- The staging runtime uses the production database, bot token, webhook secret, or storage volume.
- The production database engine is SQLite.
- TLS to PostgreSQL is unverified or plaintext.
- The backup/restore drill has not produced a restorable backup.
- The live Telegram deletion flow has not been exercised end to end.

## 7. Rollback posture

- The release is a normal application image; the previous image tag must be retained and deployed if healthchecks or smoke tests fail.
- Database migrations are additive and idempotent in this codebase; they do not drop or rewrite user data. A code rollback does not require a schema rollback for the migrations inspected here.
- `scripts/staging_rollback.py` is staging-only and refuses production environment files, production image names, and unidentifiable `local`/`latest` tags.
- Backups must be encrypted and stored off-host; the restore path is documented in `DISASTER_RECOVERY.md`.

## 8. Production status

**Production deployment: NO.**

No production database, data, infrastructure, secret, or deployment was touched. PR #18 was merged to `main`; this audit did not deploy production. The post-merge CI staging job built/validated the staging stack and images, but did not start a hosted staging runtime.

**Next action:** execute the hosted staging runtime drill and live OpenRouter/Telegram E2E gates above before any production decision.
