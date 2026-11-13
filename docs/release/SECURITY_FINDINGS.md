# AUSTRO AI — Security Findings

- **Date:** 2026-11-13
- **Audited base:** `63ae400b2a4f4310f944bb8f6b9f32f89fe53753` (PR #16)
- **Branch:** `release/post-merge-audit`
- **Production deployment:** **NO**
- **Production data/infrastructure/secrets changed:** **NO**

## 1. Summary

No **Critical** vulnerability was found. The audit found four **High** defects with privacy/availability impact and four **Medium** defects; all were fixed with regression tests. The remaining items are configuration/operational risks and external verification gaps, not open code vulnerabilities in the audited paths.

| Severity | Found | Fixed | Open code findings |
|---|---:|---:|---:|
| Critical | 0 | 0 | 0 |
| High | 4 | 4 | 0 |
| Medium | 4 | 4 | 0 |
| Low | 2 | 2 | 0 |

## 2. Fixed security-relevant findings

### SEC-01 (High) — Account deletion could claim success after a failed verification read

`PrivacyRepository.counts()` returned `{}` when the database read failed. `PrivacyService.delete_account()` interpreted that as “no rows remain” and set `verified=True`. A database error could therefore produce a false confirmation that all personal data was erased, and the Telegram handler would print “all your data was deleted”.

**Fix:** `counts()` returns `None` on error. `DeletionResult.residual` is optional; `None` means “the verification read failed, outcome unknown”. `/delete` reports an unverified deletion instead of a clean one.

**Tests:** `test_counts_returns_none_when_the_read_fails`, `test_delete_account_is_not_verified_when_the_check_read_fails`, `test_unverified_read_is_reported_as_unverified_not_as_residual`.

### SEC-02 (High) — Unreadable storage keys caused orphaned user files

If the storage-key read failed, deletion continued with an empty list, erased the rows that held the keys, and left the uploaded files on disk with no way to locate them. This defeated the user's erasure request for file content.

**Fix:** `storage_keys()` returns `None` on error; `delete_account()` aborts before deleting any row when the file list cannot be read. A failed file removal after a successful commit is reported (`files_failed`) and never described as complete deletion.

**Tests:** `test_storage_keys_returns_none_when_the_read_fails`, `test_delete_account_aborts_when_storage_keys_cannot_be_read`, `test_confirmation_does_not_claim_all_files_were_deleted`.

### SEC-03 (High) — Retention deleted same-day audit rows

The retention cutoff used an ISO `T` while SQLite's `CURRENT_TIMESTAMP` writes a space. Because `" " < "T"`, every row on the boundary day sorted before the cutoff and was deleted up to 24 hours early. This silently shortened the documented 90-day audit window.

**Fix:** the cutoff is formatted as `%Y-%m-%d %H:%M:%S`, matching the stored representation. The PostgreSQL test path also passes.

**Test:** `test_activity_purge_matches_the_current_timestamp_format`.

### SEC-04 (High) — A closed PostgreSQL connection poisoned the process permanently

A dropped socket or server restart left a closed connection in the thread-local slot. Every later statement failed and only a process restart recovered, turning a transient database blip into a persistent outage.

**Fix:** `_get_connection()` replaces a connection whose `closed` flag is set; a ROLLBACK that itself fails discards the connection so the next call reconnects.

**Tests:** `test_closed_postgres_connection_is_replaced`, `test_failed_rollback_discards_the_connection`, `test_live_postgres_connection_is_reused`, `test_successful_rollback_keeps_the_connection`.

### SEC-05 (Medium) — Unexpected exception during erasure left a partial transaction

`delete_all()` rolled back only on `DB_ERROR`. A non-database exception could escape with the deletes still uncommitted; a later unrelated commit could make a partial erasure permanent.

**Fix:** catch `Exception`, roll back, log, return `None`.

**Test:** `test_delete_all_rolls_back_on_an_unexpected_exception`.

### SEC-06 (Medium) — Concurrent learning-progress writes lost increments

The read-modify-write sequence released the lock between the read and the write. Two users' events could not mix (the key includes the owner), but two concurrent events for the same user/day could lose one increment, understating study/practice/review/assessment activity.

**Fix:** one atomic `INSERT ... ON CONFLICT ... DO UPDATE`.

**Test:** `test_concurrent_evidence_events_do_not_lose_increments`.

### SEC-07 (Medium) — Replayed review callback double-counted evidence

A duplicated Telegram callback (double tap or re-delivered update) applied the same review result twice, adding mastery evidence, review count, and attempt count for one answer.

**Fix:** `record_review()` ignores a grading when the review was already stamped `last_reviewed == today`. The review row's future schedule is unchanged; a legitimate review scheduled for tomorrow can still be graded.

**Test:** `test_replayed_review_grading_is_idempotent`.

### SEC-08 (Medium) — A measured 0% assessment was indistinguishable from “no assessment”

`assessment_score_avg=round(score, 2) if score else None` converted a real 0% into `None`, and every non-assessment event overwrote the column with `None`.

**Fix:** 0% is stored as `0.0`; the upsert uses `COALESCE` so only a new measured score replaces the stored average.

**Tests:** `test_zero_percent_assessment_is_stored_as_a_measured_zero`, `test_other_evidence_does_not_erase_the_assessment_average`.

### SEC-09 (Low) — Missing indexes on retention/audit tables

`activity_log` and `coach_logs` lacked indexes for the daily purge and per-owner reads, making a growing audit table progressively more expensive to purge and export.

**Fix:** four idempotent indexes added at database initialization.

**Test:** `test_retention_indexes_exist`.

### SEC-10 (Low) — Deletion success text was stronger than the facts

When stored files could not be removed, the handler still said all data was deleted.

**Fix:** the handler reports the number of files that remain and asks the user to contact support.

**Test:** `test_confirmation_does_not_claim_all_files_were_deleted`.

## 3. Security controls verified

| Control | Verification | Result |
|---|---|---|
| Owner scoping | Repository/service review plus cross-user tests | PASS |
| `/export` isolation | Owner-filtered table export; no foreign rows in tests | PASS |
| `/delete` confirmation | Token bound to user id, 10-minute TTL, cleared before delete; foreign/expired/replay refused | PASS |
| Transactional erasure | Children before parents; rollback on failure; post-delete verification | PASS |
| File deletion | Keys read before rows; per-source delete removes file first | PASS |
| Retention | Daily 03:10 UTC job; cutoff shape fixed; per-table disable | PASS |
| Prompt injection | Retrieved documents fenced as evidence; system/user roles separated | PASS (mitigation) |
| Upload path traversal | Storage path containment checks; filename normalization | PASS |
| Archive/XML hardening | ZIP bounds and metadata checks; `defusedxml` for EPUB XML | PASS |
| Secret redaction | Logging formatter and OpenRouter error redaction; no prompts/responses in telemetry | PASS |
| TLS to PostgreSQL | Remote host requires encryption; `verify-full` supported | PASS (configuration) |
| Production config validation | Fallback off, real OpenRouter key/model/https, webhook secret, log level | PASS |
| Staging isolation | Static comparison against production values; guard rejects shared secrets | PASS |
| Rate limiting | Per-key locks and bounded state; command/integration tests | PASS |
| Dependency vulnerabilities | `pip-audit` on runtime + dev requirements | PASS (33 packages, 0 known) |
| SAST | Bandit medium/high | PASS (0 medium/high) |
| Lint/compile | pyflakes, compileall | PASS |
| Security test selection | redaction, Docker context, architecture, production config | PASS (45 tests) |
| Repository secret scan | TruffleHog | NOT RUN locally; CI job |
| Live Telegram auth boundary | real bot token/chat | NOT RUN |
| Live provider key handling | real OpenRouter key | NOT RUN |

## 4. Dependency and secret scan evidence

`pip-audit -r requirements.txt -r requirements-dev.txt` audited 33 packages and reported:

```
No known vulnerabilities found
```

This is an advisory-database result, not a proof that no vulnerability exists. The CI `dependency-audit` job runs the same command and uploads `audit-report.json`.

TruffleHog is configured in CI with `--only-verified`; the binary was not installed on the audit host, so no local secret-scan result is claimed. No real credential was written to the repository during this audit. The staging `.env.staging` file and synthetic backup key were created as git-ignored local artifacts and removed/moved out of the working tree after the checks.

## 5. Residual security risks

1. **Telegram account is the identity boundary.** Anyone who controls the Telegram account controls the bot data. There is no second factor inside the bot.
2. **No global update-id dedupe table.** State-changing operations are idempotent where audited (habit completion, reminders, booking quota, review grading), but a future handler must implement its own guard.
3. **Prompt injection is mitigated, not eliminated.** Retrieved text is fenced and labeled as data, but a sufficiently persuasive document can still influence model output within the model's limits. No output exfiltration filter exists beyond the tool boundary.
4. **Uploads are parsed, not malware-scanned.** The extractors are size-bounded and hardened, but an anti-virus/content-disarm layer is an infrastructure decision.
5. **Rate limits are in-process.** Multiple app replicas would each enforce their own limit; a shared store is required for global limits.
6. **The scheduler assumes one instance.** Two polling replicas would duplicate reminders and retention jobs; a distributed lock is required before horizontal scaling.
7. **Production does not force PostgreSQL.** SQLite is the settings default; a production misconfiguration would use a local file database with different concurrency and file-permission properties.
8. **Polling only.** Webhook configuration is validated but no webhook receiver/secret-header verification is implemented at runtime.
9. **Telemetry is in-memory.** The 200-record ring buffer is privacy-positive but is lost on restart and is not a durable audit store.
10. **Backup encryption/restore was not exercised locally.** The CI backup job must pass and its evidence must be attached before production.

## 6. Security sign-off

**Conditional / not signed off for production.** The audited code paths have no known open High/Medium code vulnerability after the fixes, and the local security gates pass. Production sign-off still requires:

- green CI including the TruffleHog and backup/restore jobs;
- live Telegram and OpenRouter verification;
- a passing staging runtime drill;
- explicit PostgreSQL + verified TLS configuration;
- the operational risks above accepted by the release owner.

Production deployment status: **NO**.
