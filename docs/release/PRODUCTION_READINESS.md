# AUSTRO AI — Production Readiness

- **Date:** 2026-11-13
- **Audited base:** `63ae400b2a4f4310f944bb8f6b9f32f89fe53753` (PR #16)
- **Audit branch:** `release/post-merge-audit`
- **Verdict:** **NOT READY — NO-GO**
- **Production deployment performed:** **NO**
- **Production data/infrastructure/secrets changed:** **NO**

## 1. Verdict summary

The merged code is materially healthier after this audit: four High and four Medium defects were found and fixed, plus follow-up `_rollback_connection()` and `COALESCE` hardening on `arena/8aa8f7e9-austro-ai`, with 20 new regression tests (1100 passed, 143 skipped, 0 failed on Linux). The PostgreSQL suite, privacy/erasure tests, learning tests, security tests, performance benchmarks, lint, compile, SAST, and dependency audit pass.

It is still **not production-ready** for these reasons:

1. The fixes have not yet been merged into `main` (PR #17 remains open; `arena/8aa8f7e9-austro-ai` carries its fixes forward plus follow-up hardening).
2. Live Telegram end-to-end behavior was not exercised (no valid bot token or safe chat).
3. Live OpenRouter behavior was not exercised (no real API key).
4. The staging runtime drill (`staging_validate.py --execute`) was not run; only compose resolution, static isolation, image builds, and guard checks were performed.
5. Production validation does not force PostgreSQL; a deploy must explicitly set `DB_ENGINE=postgresql` or the app will use SQLite.

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
| Secret scan (TruffleHog) | **PASS (PR #17 CI)** | passed in CI run `43493434771`; re-runs on branch push |
| Backup/restore integration | **PASS (PR #17 CI)** | passed in CI run `43493434771`; re-runs on branch push |
| CI matrix (3.10–3.14) | **PASS (PR #17 CI) / PENDING BRANCH** | passed on PR #17 (`43493434771`); re-running for `arena/8aa8f7e9-austro-ai` |
| PR review/merge | **NOT DONE** | PR not yet merged into `main` |

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

1. Push `release/post-merge-audit`, open a PR, and get all GitHub CI jobs green (test matrix, PostgreSQL, backup/restore, dependency audit, performance, security, secret scan, staging compose/images).
2. Review and merge the PR through the repository's normal review process. **This audit does not merge it.**
3. Run the staging runtime drill with real staging credentials:
   `python scripts/staging_validate.py --env-file .env.staging --execute`
   and confirm cold start, migrations, healthcheck, DB identity, storage marker, restart.
4. Execute the live Telegram E2E procedure from `TELEGRAM_E2E_PROCEDURE.md` with a dedicated staging bot and test chat: `/start`, a goal flow, `/export`, `/delete` cancel/confirm, source deletion, reminder delivery.
5. Run at least one live OpenRouter generation and one provider-error/rate-limit check with a real key; confirm no secret is logged and telemetry records only metadata.
6. Run the encrypted backup and disposable restore drill against staging (or rely on the CI job with evidence attached to the release).
7. Set `DB_ENGINE=postgresql` explicitly and verify `DB_SSLMODE=verify-full` with the production CA bundle.
8. Prepare the rollback image/tag and confirm the healthcheck against the previous release.
9. Attach the CI run URL, staging drill output, and E2E evidence to the release record.

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

No production database, data, infrastructure, secret, or deployment was touched. No PR was merged, and nothing was pushed to `main`. The staging stack was not started; only compose validation and image builds were performed.

**Next action:** push the audit branch, open the PR, wait for CI, then execute the staging runtime and live E2E gates above before any production decision.
