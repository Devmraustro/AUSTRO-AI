# AUSTRO AI — Post-Merge Audit (PR #16)

- **Date:** 2026-11-13
- **Branch:** `release/post-merge-audit`
- **Verified base:** `63ae400b2a4f4310f944bb8f6b9f32f89fe53753` (`origin/main`, PR #16 squash merge)
- **Production deployment:** **NO** — no production database, data, infrastructure, secrets, or deployment were changed.
- **Merge status:** no PR was merged and nothing was pushed to `main`.

## 1. Starting point verification

The starting point was independently verified rather than assumed:

| Check | Evidence | Result |
|---|---|---|
| `origin/main` SHA | `git rev-parse origin/main` | `63ae400b2a4f4310f944bb8f6b9f32f89fe53753` |
| Commit identity | `git show -s 63ae400` | `feat: coach_logs writer + per-request AI telemetry, merged with the retention job that purges them (#16)` |
| Parent history | `git log --oneline` | `63ae400` (#16) → `40543c3` (#15) → `219c8e1` (#14) |
| Production deploy | deployment commands | none executed |
| LLM provider | source/dependency scan | OpenRouter is the only provider; no Gemini provider code or dependency |

All work was done on `release/post-merge-audit`, created directly from verified `origin/main`.

## 2. Scope and method

The audit covered the six requested areas:

- **A.** AI gateway / OpenRouter
- **B.** Privacy and memory (export, erasure, retention, ownership)
- **C.** Database and reliability (SQLite/PostgreSQL, transactions, migrations, indexes, connection lifecycle, recovery)
- **D.** Telegram runtime (handlers, confirmation callbacks, scheduler)
- **E.** Security (authentication boundaries, prompt injection, uploads, secrets, rate limits)
- **F.** Learning and coaching (mastery, spaced review, progress accounting, replay)

Additional checks: `/export`, `/delete`, ownership isolation, confirmation-token expiry/replay, transaction rollback, source/file deletion, retention purge, `coach_logs`/`activity_log`, orphaned data, docs-versus-code claims, scheduler lifecycle, Telegram callback idempotency, duplicate progress updates, and the absence/presence of an XP system.

Constraints observed: no test was skipped, weakened, or hidden with a mock to make a gate pass. Existing tests were not modified. Every code fix has a new regression test in a new test file.

## 3. Findings

No **Critical** findings were identified. Four **High**, four **Medium**, two **Low**, and one **Informational** finding were found and fixed.

| ID | Severity | Area | Finding | Regression test |
|---|---|---|---|---|
| F-01 | High | Privacy / retention | Retention cutoff used an ISO `T` while SQLite `CURRENT_TIMESTAMP` stores a space; same-day activity rows were deleted up to 24 hours early. | `test_activity_purge_matches_the_current_timestamp_format` |
| F-02 | High | Privacy / erasure | A failed post-delete verification read was reported as a **clean** deletion because `counts()` returned `{}` on error. | `test_counts_returns_none_when_the_read_fails`, `test_delete_account_is_not_verified_when_the_check_read_fails` |
| F-03 | High | Privacy / files | If storage keys could not be read, the database rows were still erased, leaving uploaded files unreachable orphans. | `test_storage_keys_returns_none_when_the_read_fails`, `test_delete_account_aborts_when_storage_keys_cannot_be_read` |
| F-04 | Medium | Privacy / transactions | A non-database exception during `delete_all` could escape without rollback, leaving a partial transaction that a later commit could make permanent. | `test_delete_all_rolls_back_on_an_unexpected_exception` |
| F-05 | Medium | Learning / concurrency | `LearningProgressRepository.record()` read the day's row outside the write lock, so concurrent evidence events lost increments. | `test_concurrent_evidence_events_do_not_lose_increments` |
| F-06 | Medium | Learning / progress | Any non-assessment event overwrote `assessment_score_avg` with `None`; a measured 0% was also stored as `NULL`. | `test_other_evidence_does_not_erase_the_assessment_average`, `test_zero_percent_assessment_is_stored_as_a_measured_zero` |
| F-07 | Medium | Learning / Telegram replay | Replaying a review-grading callback added a second mastery evidence point, a second review count, and a second attempt. | `test_replayed_review_grading_is_idempotent` |
| F-08 | High | Database / availability | A closed PostgreSQL connection stayed in the thread-local slot forever; every later statement failed and only a process restart recovered. | `test_closed_postgres_connection_is_replaced`, `test_failed_rollback_discards_the_connection` |
| F-09 | Low | Database / performance | `activity_log` and `coach_logs` had no index for the retention filter or per-owner reads. | `test_retention_indexes_exist` |
| F-10 | Low | Telegram / honesty | `/delete` said “all your data was deleted” even when stored files could not be removed. | `test_confirmation_does_not_claim_all_files_were_deleted` |
| F-11 | Info | Documentation | `PRIVACY_RETENTION.md` claimed portability was unavailable while `/export` already provided it. | documentation review |

### F-01 — Retention deleted same-day rows early

`activity_log.timestamp` defaults to `CURRENT_TIMESTAMP`, which writes `YYYY-MM-DD HH:MM:SS`. The cutoff was built with `.isoformat()`, producing `YYYY-MM-DDTHH:MM:SS`; because `" " < "T"` in lexicographic comparison, rows on the boundary day compared smaller than the cutoff. A reproduction with one row inside and one row outside the window deleted two rows instead of one. The fix uses the same `%Y-%m-%d %H:%M:%S` shape. `coach_logs` remains date-only and unchanged.

### F-02 / F-03 — False “verified” deletion and orphaned files

`PrivacyRepository.counts()` returned `{}` on a database error, which `delete_account` interpreted as “no rows remain”. It now returns `None`, and the deletion result distinguishes:

- `verified=True`, `residual={}` — the re-read succeeded and found nothing;
- `verified=False`, `residual={...}` — the re-read succeeded and found rows;
- `verified=False`, `residual=None` — the re-read itself failed, so the outcome is unknown.

Similarly, `storage_keys()` now returns `None` on error and `delete_account` aborts before deleting rows when the file list cannot be read, because the keys would otherwise be destroyed with the rows.

### F-04 — Partial transaction on unexpected error

The bare `except DB_ERROR` path rolled back, but an unexpected driver/proxy exception escaped. `delete_all` now catches `Exception`, rolls back, logs, and returns `None` (“nothing was deleted”) instead of leaving an uncommitted partial transaction.

### F-05 / F-06 — Lost learning updates and erased averages

`record()` now performs one atomic `INSERT ... ON CONFLICT (owner_user_id, record_date) DO UPDATE`:

- counters are incremented in the database, not by read-modify-write;
- `assessment_score_avg` uses `COALESCE(excluded.assessment_score_avg, learning_progress.assessment_score_avg)`, so non-assessment events cannot erase a measured average;
- a measured 0% is passed as `0.0`, not `None`.

The target column is qualified (`learning_progress.study_minutes`) because PostgreSQL rejects the unqualified form as ambiguous; SQLite accepts both. This difference was caught by the real PostgreSQL suite, not by SQLite.

### F-07 — Replayed review grading

`record_review()` now ignores a grading for a review already stamped `last_reviewed == today`. A review legitimately scheduled for tomorrow can still be graded (existing behavior relied on by the suite); only a same-day replay is refused. This covers both a double-tapped button and a callback re-delivered after an unconfirmed poll offset.

### F-08 — Permanently poisoned PostgreSQL connection

`DatabaseManager._get_connection()` now replaces a PostgreSQL connection whose `closed` flag is set, and `_discard_connection()` is called whenever a ROLLBACK itself fails. The next statement reconnects instead of reusing a dead session for the life of the process.

### F-09 — Missing retention/read indexes

Added `idx_activity_log_timestamp`, `idx_activity_log_user`, `idx_coach_logs_date`, and `idx_coach_logs_user_date`. They are created idempotently on every database initialization, so existing databases receive them on the next start, not only fresh installs.

### F-10 — Success message stronger than the facts

When `files_failed > 0`, the handler now reports that database records were deleted but N stored files were not, and asks the user to contact support. It never claims “all data” in that case.

## 4. Area-by-area verification

### A. AI gateway / OpenRouter

- **Provider:** OpenRouter is the only configured AI provider. No Gemini provider, dependency, or fallback remains.
- **Separation of roles:** the system prompt and user message are sent as separate message roles; retrieved documents are fenced as evidence, not instructions (`app/knowledge/rag.py`, `app/knowledge/models.py`).
- **Telemetry:** per-run metadata only (capability, provider, model, latency, success, retries, tokens, cost, error). Prompts, responses, message text, API keys, and bot tokens are not recorded. Token/cost extraction rejects booleans, non-finite values, negatives, and overflow.
- **Failure handling:** retry/failover and quota handling are covered by the offline suite and the local-fallback performance benchmark.
- **Not verified live:** no real `OPENROUTER_API_KEY` was available, so no billable provider call or live provider-error matrix was executed. This is an external dependency, not a passing claim.

### B. Privacy and memory

- `/export` and `/delete` are registered commands; `/export` is sent as a JSON document and includes every table returned by `PrivacyRepository.export_rows`, plus learning and memory facades when wired.
- `/delete` requires an explicit button; the token is bound to the user id, has a 10-minute TTL, and is cleared before deletion. Foreign, expired, replayed, and missing tokens are refused.
- Erasure is one transaction, children before parents, with the `users` row last. The erasure table lists are checked by a test that requires every table carrying `user_id`/`owner_user_id` to be covered.
- `activity_log` has no foreign key and is explicitly deleted; `knowledge_citations` are removed indirectly through their retrieval events.
- Per-source deletion (`kdel:<id>`) removes the stored file before the row and refuses to keep a half-deleted source.
- Retention is enforced daily at 03:10 UTC by `retention_purge`; `ACTIVITY_LOG_RETENTION_DAYS` and `COACH_LOG_RETENTION_DAYS` default to 90 and `0` disables a table's purge.
- Documentation was corrected where it contradicted the implemented export capability.

### C. Database and reliability

- The same repository/service paths run against SQLite 3.50.4 and PostgreSQL 16 in this audit.
- Dialect translation covers `?` → `%s`, `INSERT OR REPLACE/IGNORE`, scalar `MAX` → `GREATEST`, `lastrowid` via `RETURNING`, boolean normalization, and row compatibility.
- Migrations are recorded in `schema_migrations`, applied transactionally, and idempotent; index creation is re-run safely at startup.
- Transactions are committed explicitly; failure paths roll back. The new connection-lifecycle fix removes the permanent-failure mode after a dropped socket.
- Concurrency: atomic habit completion, booking/quota paths, and the new learning-progress upsert are covered by SQLite and PostgreSQL tests.
- Scheduler lifecycle: the reminder scheduler is created in `post_init` and stopped in `post_stop`; the daily retention job is registered in the same lifecycle. The live long-polling process was not started because no bot credentials were provided.

### D. Telegram runtime

- Handler registration is covered by the suite, including callback patterns for privacy actions.
- Duplicate-relevant domain operations are idempotent: habit completion per day, delivered-reminder markers/attempt caps, booking quota, and now review grading.
- There is no global `update_id` dedupe table. Duplicate Telegram deliveries are handled at the domain-operation level where the operation changes state; a future global dedupe would be an additional defense, not a replacement for these guards.
- Live Telegram E2E (real bot, real chat, real callback taps) was **not executed**; no valid bot token or safe test chat was provided.

### E. Security

- Ownership isolation is enforced in repository queries and service entry points; deletion and export are owner-scoped.
- Deletion confirmation is user-bound, expiring, one-time, and cleared before the destructive call.
- Uploads: path traversal is rejected, extraction is size-bounded, ZIP metadata is validated, and EPUB/XML parsing uses hardened parsers.
- RAG treats retrieved text as untrusted data; the system prompt explicitly fences it as evidence.
- Secrets are redacted from logs; API keys appear only in headers and are removed from provider error text.
- TLS: remote PostgreSQL hosts require an encrypted connection; production/staging validation requires HTTPS OpenRouter endpoints.
- Rate limiting, webhook secret configuration, and production configuration validation are covered by the offline suite.
- The repository secret scan (TruffleHog) is a CI job; it was not available locally. Bandit, pyflakes, compileall, the security test selection, and the dependency audit were run locally.

### F. Learning and coaching

- Mastery, spaced-review scheduling, assessment grading, misconceptions, learning plans, and progress accounting are covered by the offline suite.
- Progress counters are now atomic and do not lose concurrent evidence.
- A measured 0% is distinguishable from “no assessment”.
- Replayed grading no longer double-counts mastery evidence or review counts.
- **There is no XP system in this codebase.** The product tracks mastery states, review counts, study minutes, practice counts, assessment counts/scores, streaks, and plan completion — not XP. No XP feature was claimed or invented.
- The evaluation harness (`scripts/verify.py`) passed 100% against its local thresholds.

## 5. Test and gate evidence

Environment: Windows 11, Python 3.14.7, SQLite 3.50.4, PostgreSQL 16 in a disposable Docker container (`austro-pg-audit`, port 55432), Docker 29.9.2.

| Gate | Command | Result | Exit |
|---|---|---|---|
| Full SQLite suite (baseline, before fixes) | `pytest -q` | `2 failed, 1079 passed, 142 skipped` in 799.40s | 1 |
| Full SQLite suite (final code) | `pytest -q` | **`2 failed, 1097 passed, 142 skipped` in 800.33s** | 1 |
| PostgreSQL repositories | `pytest tests/test_repositories_pg.py -q` | `30 passed` in 39.71s | 0 |
| PostgreSQL claim mechanism | `pytest tests/test_claim_mechanism_pg.py -q` | `7 passed` in 183.78s | 0 |
| New privacy regressions | `pytest tests/test_privacy_post_merge_regressions.py -q` | `10 passed` | 0 |
| New learning/DB regressions | two new test modules | `8 passed` in 7.28s | 0 |
| Security tests from CI | `test_redaction`, `test_docker_context`, `test_architecture`, `test_production_config` | `45 passed` in 45.03s | 0 |
| Evaluation harness | `python scripts/verify.py` | `OVERALL: PASS (100.0% / 100% threshold)` | 0 |
| Offline smoke | `python smoke_test.py` | `SMOKE TEST PASSED` | 0 |
| Performance | `python tests/test_performance.py` | `ALL PASS: 7/7 benchmarks` | 0 |
| Lint | `python -m pyflakes ...` | no findings | 0 |
| Compile | `python -m compileall -q ...` | success | 0 |
| Bandit SAST | `bandit -r app/ --severity-level medium --confidence-level medium` | no medium/high issues (13 low) | 0 |
| Dependency audit | `pip-audit -r requirements.txt -r requirements-dev.txt` | 33 dependencies, no known vulnerabilities | 0 |
| Staging compose | `docker compose ... config -q` | resolves; service `app` | 0 |
| Staging static isolation | `python scripts/staging_validate.py --env-file .env.staging --static` | `PASS: 4 credential(s) differ from production` | 0 |
| Shared-secret rejection | temp env comparison | `FAIL: staging reuses production value(s) for: BOT_TOKEN` as designed | 0 (expected rejection) |
| Rollback guards | `staging_rollback.py --plan` × 3 | all three refused (production env, production image, unidentifiable `local` tag) | 2 each (expected) |
| Staging app image | `docker compose ... build app` | built `austro-staging-app:local` | 0 |
| Staging backup image | `docker compose ... build backup` | built `austro-staging-backup:local` | 0 |
| Backup image `pg_dump` | `docker compose ... run --rm backup -c "pg_dump --version"` | `pg_dump (PostgreSQL) 16.16` | 0 |

The only two failures in the full SQLite suite are the known Windows-only environment failures:

1. `tests/test_backup_deployment.py::test_deprecated_backup_wrapper_fails_closed_without_key` — shell/path behavior on Windows.
2. `tests/test_learning_flow_regressions.py::test_every_button_in_the_bot_has_a_handler` — source read uses the Windows `cp1252` default encoding and fails on Arabic text. Linux CI reads UTF-8.

Neither is a product failure under the Linux CI environment, but the local full-suite exit code is **1**, and this report does not claim otherwise.

## 6. Changed files

Modified:

- `app/application/privacy.py` — `residual` may be unknown; abort on unreadable storage keys; unverified-read semantics.
- `app/database/connection.py` — reconnect after a closed PostgreSQL connection; retention/read indexes.
- `app/database/repositories.py` — `counts()`/`storage_keys()` return `None` on error; retention timestamp format; unexpected-exception rollback; rollback discards a dead connection.
- `app/knowledge/repositories.py` — rollback discards a dead connection.
- `app/learning/assessment.py` — measured 0% stored as `0.0`.
- `app/learning/engine.py` — same-day replay guard for review grading.
- `app/learning/repositories.py` — atomic progress upsert with `COALESCE`; rollback recovery.
- `app/memory/repositories.py` — rollback discards a dead connection.
- `app/telegram/privacy_handlers.py` — refuse summary-less deletion offers; distinguish unverified reads; report file failures.
- `PRIVACY_RETENTION.md` — corrected the portability claim.

Added:

- `tests/test_privacy_post_merge_regressions.py` (10 tests)
- `tests/test_learning_progress_post_merge_regressions.py` (4 tests)
- `tests/test_db_recovery_post_merge_regressions.py` (4 tests)
- `docs/release/POST_MERGE_AUDIT.md`, `PRODUCTION_READINESS.md`, `SECURITY_FINDINGS.md`, `EXTERNAL_DEPENDENCIES.md`

## 7. What was not verified

- Live Telegram E2E — no valid bot token, no safe test chat.
- Live OpenRouter generation/error/quota behavior — no real API key.
- Staging **runtime** cold start, healthcheck, DB identity, storage marker, and restart drill — compose and images were validated, but the full `--execute` drill was not run.
- Python 3.10–3.13 locally; only 3.14.7 was available. CI is configured to run the matrix.
- TruffleHog secret scan locally; it is a CI job.
- Encrypted backup/restore integration locally; CI is configured for it.
- Production database backup/restore drill; no production access was used.

## 8. Remaining risks

1. The fixes are on an unmerged branch and have not run through GitHub CI at the time of writing.
2. The local full suite exits 1 because of the two Windows-only environment failures; CI must confirm zero failures on Linux.
3. Live Telegram and live OpenRouter behavior remain externally unverified.
4. Staging was validated statically and built as images, not exercised as a running stack.
5. Production validation does not force PostgreSQL; a production deployment must explicitly set `DB_ENGINE=postgresql` and verified credentials, or it will use SQLite.
6. The runtime supports only Telegram long polling; webhook configuration is validated but not implemented as a transport.
7. The scheduler assumes one polling application instance; horizontal scaling would require a distributed reminder lock.
8. No XP system exists; progress reporting must use the mastery/learning counters above.

## 9. Deployment status

- Production deployment: **NOT PERFORMED — NO**.
- Production database/data/infrastructure/secrets changed: **NO**.
- PR merged: **NO**.
- Push to `main`: **NO**.
- Staging deployment: images built; runtime drill not executed.
