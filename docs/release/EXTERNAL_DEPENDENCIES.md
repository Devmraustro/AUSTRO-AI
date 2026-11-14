# AUSTRO AI — External Dependencies and Verification Status

- **Date:** 2026-11-13
- **Audited base:** `63ae400b2a4f4310f944bb8f6b9f32f89fe53753`
- **Audit branch:** `release/post-merge-audit`
- **Production deployment:** **NO**

This document separates what was **locally verified** from what is **awaiting credentials**, **partial**, **missing**, or **blocked by infrastructure**. No external result is presented as passing when it did not run.

## 1. Status summary

| Dependency / service | Purpose | Status | Evidence |
|---|---|---|---|
| OpenRouter API | Only LLM provider | **Awaiting credentials** | Code path, retry/quota/telemetry tests, local fallback verified; no live key |
| Telegram Bot API | Runtime transport and live E2E | **Awaiting credentials** | Offline handler/callback tests pass; no live bot/chat |
| PostgreSQL 16 | Production-grade database | **Locally verified (disposable)** | 30 repository + 7 claim tests against `austro-pg-audit:55432` |
| Production PostgreSQL instance | Live production database | **Not accessed** | No production host/credentials used |
| Docker Engine | Image build and staging checks | **Locally verified (build)** | Docker 29.9.2; staging app image built; compose resolves |
| Hosted staging environment | Runtime cold start/health/restart | **Blocked by infrastructure** | No staging host; only local static + build checks |
| `pip-audit` advisory database | Dependency vulnerabilities | **Locally verified** | 33 packages, no known vulnerabilities, exit 0 |
| TruffleHog | Secret scanning | **Passed in PR #17 CI / missing locally** | Binary not installed locally; passed in CI run `43493434771` (`release/post-merge-audit`) and re-runs on push |
| Encrypted backup + restore | Disaster recovery | **Passed in PR #17 CI / not locally run** | Passed in CI run `43493434771` (PG 16 service); re-runs on push |
| GitHub Actions CI | Full matrix and gates | **Passed on PR #17 / pending push on this branch** | PR #17 run `43493434771` green across all 9 jobs; re-running on `arena/8aa8f7e9-austro-ai` push |
| GitHub CLI authentication | PR creation/monitoring | **Blocked (invalid gh token)** | `gh auth status` reports invalid keyring token; git remote access still works |

## 2. OpenRouter

- **What is implemented:** one provider (`app/ai/openrouter.py`), HTTPS-only validation, key only in the `Authorization` header, provider-error redaction, retry/failover through the gateway, quota snapshot validation, and metadata-only telemetry.
- **What was verified locally:** provider selection, no Gemini fallback, retry/fallback behavior, quota parsing, token/cost validation, telemetry content, and the evaluation harness with the local path. The performance suite recorded `AI Gateway Local Fallback` at 1.9 ms (threshold 100 ms).
- **What was not verified:** a real generation, real provider error codes, real quota exhaustion, and real model output quality. These require `OPENROUTER_API_KEY` and would spend money.
- **Blocking level:** release blocker for production sign-off, not for merging the audit branch.
- **Required to close:** set a real key in a staging environment, run one successful generation and one controlled error/rate-limit case, confirm logs/telemetry contain no prompt or secret.

## 3. Telegram Bot API

- **What is implemented:** long polling (the only supported transport), command registration, conversation handlers, callback query handling, privacy confirmation tokens, reminder scheduler in `post_init`/`post_stop`, and rate-limit wrappers.
- **What was verified locally:** handler registration, callback patterns, privacy flows, scheduler unit tests, reminder idempotency, and the offline smoke test. No network calls were made.
- **What was not verified:** a live `/start`, real button taps, replayed update delivery by Telegram, real file upload/download, and deletion against a real chat. `TELEGRAM_E2E_PROCEDURE.md` defines the procedure; it was not executed.
- **Blocking level:** release blocker for production sign-off.
- **Required to close:** a dedicated staging bot token and a safe test chat, then execute the E2E procedure and attach the transcript/IDs (redacted).

## 4. PostgreSQL

- **What was verified locally:** a disposable PostgreSQL 16 container (`austro-pg-audit`, port 55432) with a dedicated role/database. The exact CI repository suite passed `30 passed` in 39.71 s and the claim suite passed `7 passed` in 183.78 s, neither skipped. The logic under test includes dialect translation, upserts, `RETURNING`, booleans, date cutoffs, row parity, privacy deletion/export, and learning progress.
- **Bug found by this dependency:** the atomic learning-progress upsert used an unqualified target column. SQLite accepted it; PostgreSQL rejected it as ambiguous. The SQL was corrected and the PostgreSQL test now passes.
- **What was not verified:** a production PostgreSQL instance, its TLS certificate chain, its backup/restore, its connection pool under sustained load, and a real failover/restart.
- **Blocking level:** configuration/production verification blocker.
- **Required to close:** production `DB_ENGINE=postgresql`, verified TLS (`DB_SSLMODE=verify-full` + CA bundle), connection test, and a restore drill.

## 5. Docker and staging

- **What was verified locally:**
  - `docker compose -f docker-compose.staging.yml -p austro-staging --env-file .env.staging config -q` → exit 0, service `app`.
  - Static staging isolation → `PASS: 4 credential(s) differ from production`.
  - Shared-secret guard → correctly returned `FAIL: staging reuses production value(s) for: BOT_TOKEN` in the synthetic comparison.
  - Rollback guards → production env, production image, and `local` tag all refused with exit 2.
  - Staging app image → `austro-staging-app:local` built successfully.
  - Staging backup image → built successfully (`BUILD_EXIT=0`); `pg_dump --version` reports `pg_dump (PostgreSQL) 16.16`, matching the PostgreSQL 16 target.
- **What was not verified:** a running staging stack: cold start, migrations, healthcheck, DB identity, storage marker, restart, and smoke test inside the container. This requires `staging_validate.py --execute` on a Docker host with a staging database and real staging credentials.
- **Blocking level:** release blocker for production sign-off; not a code-merge blocker.
- **Required to close:** run the full staging drill and attach its output.

## 6. Secret scanning

- **Configured:** the CI workflow runs `trufflesecurity/trufflehog@v3.97.9` with `--only-verified` against the PR base/head.
- **Local status:** the binary is not installed, so no local secret scan is claimed.
- **Blocking level:** CI gate; must pass before merge/release.
- **Required to close:** push the branch and confirm the TruffleHog job is green.

## 7. Backup and restore

- **Configured:** `Dockerfile.backup`, `scripts/ci_backup_fixture.py`, and `scripts/ci_backup_integration.sh` run in the `backup-integration` CI job: encrypted backup, wrong-key refusal, tamper refusal, unsafe-target refusal, restore into a disposable database, and cleanup.
- **Local status:** not executed. It requires a Linux/bash environment and a disposable PostgreSQL service; the local Windows audit host did not run that workflow.
- **Blocking level:** production-readiness blocker; CI evidence is required.
- **Required to close:** green backup-integration job with the restore assertion, plus a production-like restore drill before go-live.

## 8. GitHub / CI

- **Configured:** `.github/workflows/ci.yml` has jobs for the 3.10–3.14 test matrix, PostgreSQL integration, encrypted backup/restore, dependency audit, performance, security tests, secret scan, and staging compose/image validation.
- **Status:** PR #17 (`release/post-merge-audit`) completed CI run `43493434771` with all 9 jobs passing (`test (3.10..3.14)`, `Staging stack (compose + images)`, `Encrypted backup and disposable restore (PostgreSQL 16)`, `dependency-audit`, `performance-tests`). Branch `arena/8aa8f7e9-austro-ai` carries those fixes forward with follow-up `_rollback_connection()` and `COALESCE` hardening and awaits its own CI run on push.
- **Blocking level:** merge blocker until CI is green on the target branch; production blocker until the release record includes the final merged CI URL.
- **Required to close:** push `arena/8aa8f7e9-austro-ai`, verify the CI run is green, and record the CI run URL and SHA.

## 9. Things intentionally not attempted

- Production deployment, production migrations, production database queries, and production secret changes.
- Merging any PR or pushing to `main`.
- Live Telegram messages to real users.
- Billable OpenRouter requests without an approved key/budget.
- Destructive restore against anything other than a disposable database.

## 10. Minimum external inputs needed to finish verification

1. `OPENROUTER_API_KEY` (staging/test budget) and an approved model.
2. A dedicated staging `BOT_TOKEN` and a safe test chat ID.
3. A staging PostgreSQL 16 endpoint with TLS and disposable restore target.
4. A staging host (or permission to run the compose stack locally with real staging credentials).
5. Re-authenticated GitHub CLI (or another approved way to open the PR).
6. Approval to run the CI backup/restore job and attach its evidence.

Until these are provided, the corresponding rows above remain **awaiting credentials**, **partial**, **missing**, or **blocked by infrastructure** — never “passed”.
