# AUSTRO AI — Code Audit Report

- **Branch:** `main`
- **Baseline commit:** `c8066f4ea9a97feed95a6c802e8cfbc477cba59f`
- **Audit commits:** `c8066f4` (post-push correction pass), `f8c29ae`, `08e923a`
- **CI:** [run `37934832595`](https://github.com/Devmraustro/AUSTRO-AI/actions/runs/37934832595) on `a218fd2` — all jobs success
- **Scope:** full local security / correctness / runtime-readiness review of the
  repository at the baseline commit. No hosting, deployment, provisioning, or
  live-service access was performed or attempted.
- **Status:** 17 defects fixed (F-1 through F-17); residual risks R-2, R-4 and
  R-5 resolved and locally verified with regression tests; R-1 and R-3 remain
  open. The pre-existing `register_upload` async mismatch (4 failing upload
  tests) is fixed — local suite 479 passed / 19 skipped / 0 failed, and CI run
  `37934832595` is green on every job (Python 3.10–3.14, Bandit, PostgreSQL
  integration, dependency audit, Docker/staging, performance, secret scan).

This report does not claim the absence of vulnerabilities. Absence of findings
is not proof of absence.

---

## 1. Method and tooling

Every finding below was reproduced against the baseline code before a fix was
written. Findings without a reproduction are labelled as such and are *not*
counted as fixed.

| Check | Command | Result |
|---|---|---|
| Byte-compile | `python -m compileall -q app main.py handlers.py config.py database.py reminder_scheduler.py redaction.py smoke_test.py scripts tests` | exit 0 |
| Lint | `python -m pyflakes main.py handlers.py config.py database.py reminder_scheduler.py redaction.py app tests scripts/staging_validate.py scripts/staging_rollback.py` | exit 0, no findings |
| Whitespace | `git diff --check` | exit 0 |
| Dependency integrity | `python -m pip check` | `No broken requirements found.` |
| Dependency vulnerabilities | `python -m pip_audit -r requirements.txt -r requirements-dev.txt` | `No known vulnerabilities found` |
| SAST | `bandit -r app/ --severity-level medium --confidence-level medium` | `No issues identified`, exit 0 |
| Evaluation harness | `python scripts/verify.py` | `OVERALL: PASS (100.0% / 100% threshold)`, exit 0 |
| Offline smoke | `python smoke_test.py` | `SMOKE TEST PASSED`, exit 0 |
| Unit + integration tests | see §2 | 479 passed, 19 skipped, 0 failed |

Local interpreter: CPython 3.14.7 on Windows. CI runs CPython 3.10/3.11/3.12/3.13/3.14
on `ubuntu-latest`, which is the authoritative Python matrix for this project.

> Environment note (local only, not CI): the autouse `fresh_db` fixture re-runs
> the full `init_database()` DDL migration chain for every test, which takes
> ~10s per call on this machine (slow disk + SQLite per-statement fsync). The
> full local suite therefore takes roughly an hour; GitHub Actions runs the same
> suite on `ubuntu-latest` where this cost is negligible. Results below were
> collected with pytest 9.1.1 / pytest-asyncio 1.4.0 using isolate-scoped
> `--basetemp` under `%TEMP%`.

**Not available locally:** Docker/Compose and a PostgreSQL server. Docker and
PostgreSQL are exercised in GitHub Actions instead. Bandit is now installed
locally and runs fail-closed with the same flags as CI.

---

## 2. Test results

The complete suite was run locally with the CI commands (`compileall`,
`pyflakes`, `pytest`, `pip check`, `pip-audit`, Bandit, `scripts/verify.py`,
`smoke_test.py`). The tables below separate **local results**, **GitHub Actions
results**, and **environment-dependent checks**.

### 2a. Local results (CPython 3.14.7 on Windows)

| Batch | Result |
|---|---|
| `tests/test_rate_limit_guards.py` + `test_rate_limit_integration.py` + `test_rate_limiter.py` + `test_rate_limit_commands.py` | 115 passed |
| `tests/test_memory.py` | 60 passed |
| `tests/test_backup.py` | 47 passed (16 encryption-specific) |
| `tests/test_database.py` + `test_redaction.py` | 11 passed |
| `tests/test_audit_regressions.py` | 63 passed |
| `tests/test_architecture.py` | 39 passed |
| `tests/test_learning.py` + `test_knowledge.py` + `test_rag.py` | 50 passed |
| `tests/test_handlers.py` + `test_scheduler.py` + `test_smoke.py` + `test_flashcards.py` | 20 passed |
| `tests/test_staging.py` + `test_production_config.py` + `test_docker_context.py` | 74 passed, 1 skipped |
| `tests/test_performance.py` + `test_repositories_pg.py` | 18 skipped (no PG/performance env locally) |
| **Total** | **479 passed, 19 skipped, 0 failed** |

The four tests that failed in CI runs #23/#24 now pass individually and as part
of the full suite:

1. `test_learning.py::test_book_course_creates_curriculum_from_source` — PASSED
2. `test_rate_limit_guards.py::test_allowed_upload_registers_and_schedules_once` — PASSED
3. `test_rate_limit_guards.py::test_background_ingestion_is_not_charged_again` — PASSED
4. `test_rate_limit_integration.py::test_allowed_upload_registers_and_starts_ingestion` — PASSED

### 2b. Root cause and fix (upload async contract)

`KnowledgeService.register_upload` is `async def` by design (the docstring and
implementation run the blocking checksum/storage/database work through
`asyncio.to_thread`). Its only production caller, `app/telegram/handlers.py`
(`knowledge_document`), correctly awaits it. The four failures were a **test
side** contract mismatch, not a production defect:

- Two test doubles patched `register_upload` with a **sync** `Mock(return_value=dict)`
  (`tests/test_rate_limit_guards.py`, `tests/test_rate_limit_integration.py`).
  The handler still awaits the call, so `await <dict>` raised
  `'dict' object can't be awaited` and the handler aborted before scheduling
  background ingestion — making the registration/task/single-charge assertions
  fail. Fix: those doubles are now `AsyncMock(return_value={...})`, matching the
  real async API.
- Two async tests called the **real** `register_upload` without awaiting it
  (`tests/test_learning.py`, `tests/test_repositories_pg.py`), then treated the
  coroutine object as a dict. Fix: added `await` at both call sites.

Regression tests added in `tests/test_rate_limit_guards.py` (upload/ingestion
section) for the properties the failing tests did not fully pin down:

- `test_allowed_upload_passes_source_id_to_background_ingestion` — the source id
  returned by registration is the one passed to background ingestion;
- `test_duplicate_upload_does_not_schedule_ingestion` — a checksum duplicate is
  reported but never re-ingested and creates no background task;
- `test_upload_validation_error_aborts_with_message_only` — a `ValidationError`
  from registration replies with the validation message and schedules nothing;
- `test_upload_registration_error_aborts_without_scheduling` — a generic
  storage/database failure replies with the generic error and schedules nothing.

The remaining eight required outcomes (dict-not-coroutine return, register-once,
rate-limit-before-download, no orphan source on blocked upload, single background
task per accepted non-duplicate upload, no second ingestion-charge in the
background task, safe registration-error handling) are enforced by the combined
pre-existing and new assertions above.

### 2c. GitHub Actions results

Run [`37934832595`](https://github.com/Devmraustro/AUSTRO-AI/actions/runs/37934832595)
on commit `a218fd23315cf2dd467b04629d0f5ce364dfd0d7` — status **success**
(all jobs green; `head_sha` matches the pushed commit):

| Job | Result | Duration |
|---|---|---|
| `test (3.10)` | success | 130s |
| `test (3.11)` | success | 107s |
| `test (3.12)` | success | 114s |
| `test (3.13)` | success | 127s |
| `test (3.14)` | success | 132s |
| `Staging stack (compose + images)` | success | 51s |
| `dependency-audit` | success | 19s |
| `performance-tests` | success | 12s |

Within each `test` job every step ran to `success` — none skipped: byte-compile,
pyflakes, `Run tests` (`pytest -v`, all matrix versions), Bandit, offline smoke,
PostgreSQL integration tests (the step's own gate requires `18 passed` with no
`skipped`, so PG ran 18/18), evaluation regression harness (`scripts/verify.py`),
security tests, and the TruffleHog repository secret scan.

Previous run `37773381695` failed only at `test (3.11) :: pytest -v` on the four
tests fixed in §2a; its `dependency-audit`, `performance-tests` and
`Staging stack` jobs passed.

### 2d. Environment-dependent checks

- PostgreSQL integration (`test_repositories_pg.py`, 18 tests) — skipped
  locally; exercised in the CI `test` job with a `postgres:16` service.
- Docker/Compose staging — not run locally; exercised in the CI `staging` job.
- `test_performance.py` — skipped locally; exercised in the CI
  `performance-tests` job.

---

## 3. Findings fixed

Severity is impact-based: **Critical** = silent, irreversible data loss or
guaranteed credential/privacy exposure on a normal path; **High** = a real
security or correctness failure reachable by ordinary use; **Medium** =
hardening or a defect requiring unusual conditions.

### F-1 · Critical · `INSERT OR REPLACE` on `users` destroyed all user data

**File:** `app/database/repositories.py` (`UserRepository.create`)

`users` is the parent of eleven `ON DELETE CASCADE` foreign keys. In SQLite,
`INSERT OR REPLACE` is implemented as DELETE-then-INSERT, so every
re-registration deleted the row and cascaded to every child table. `/start`
runs on each session start, so this fired routinely.

Only `user_id, username, first_name, last_name, last_active` were re-inserted,
so `age`, `education_level`, `goals`, `current_skills`, `daily_available_time`,
`strengths`, `weaknesses`, `current_mode` and `created_at` were all silently
reset — and because `age` was reset, the wipe was self-perpetuating.

Reproduction at the baseline: create a user, add 2 goals and 1 habit, set
`age=27`/`education_level=CS`, then call `create_user` again.

```
before re-register : goals=2 habits=1
after  re-register : goals=0 habits=0   age=None education_level=None
```

**Fix:** replaced with a true `ON CONFLICT (user_id) DO UPDATE` upsert that
updates only the five identity columns. Valid on both SQLite (>= 3.24) and
PostgreSQL.

### F-2 · Critical · No rollback after a failed statement (123 call sites)

**Files:** `app/database/repositories.py`, `app/knowledge/repositories.py`,
`app/memory/repositories.py`, `app/learning/repositories.py`

The repository layer runs multi-statement operations and catches `DB_ERROR` to
degrade gracefully, but never issued a `ROLLBACK`. Two consequences:

- SQLite keeps the implicit transaction open, so a partially-applied write is
  committed later by an unrelated `commit()` in a different code path.
- PostgreSQL poisons the session: after any statement error every subsequent
  command fails with `25P02 InFailedSqlTransaction` until a `ROLLBACK` is
  issued, turning one transient error into a total outage for that thread's
  connection.

Only `apply_migrations()` ever called `conn.rollback()`.

**Fix:** added a `_rollback()` helper to each repository base class
(`_BaseRepository`, `_KnowledgeBase`, `_MemoryBase`, `_LearningBase`) and
invoked it as the first statement of all 125 `except DB_ERROR` blocks. The
helper swallows its own exceptions so a rollback failure never masks the
original error. All call sites are inside the same `with self._manager._lock`
block as the statement that failed. `test_every_db_error_handler_rolls_back`
enforces this across the layer, so a newly added method cannot reintroduce the
bug.

### F-3 · High · Daily reviews were appended, not upserted

**File:** `app/database/repositories.py` (`ReviewRepository.save`),
`app/database/migrations.py`

`daily_reviews` has no unique constraint, so the `ON CONFLICT`/`INSERT OR
REPLACE` upsert could never conflict and every save appended another row.
Reproduction: three saves for one `(user_id, date)` produced 3 rows, making
`get_daily_reviews` and the `COUNT(*)` in `get_dashboard_stats` wrong.

**Fix:** added migration `CORE_V1` (schema `core`, version 1) which collapses
pre-existing duplicates (keeping the newest row per user/date) and creates
`uq_daily_reviews_user_date` and `uq_progress_user_date`. The save path is now
an explicit `ON CONFLICT (user_id, date) DO UPDATE`. The dedupe statements are
idempotent and are a no-op on a healthy database.

### F-4 · High · Lost update on daily progress

**File:** `app/database/repositories.py` (`ProgressRepository.log`)

A `SELECT` then conditional `UPDATE`/`INSERT`. The manager lock only serialises
threads inside one process, so a second process or any code path bypassing the
lock could interleave between the two statements, duplicating or dropping the
day's row. With no unique index there was nothing to prevent it.

**Fix:** a single `INSERT ... ON CONFLICT (user_id, date) DO UPDATE` relying on
the `uq_progress_user_date` index added by `CORE_V1`. Only the supplied
columns are updated, so an untouched field is no longer reset. Empty `kwargs`
is now an explicit no-op.

### F-5 · Critical · Unbounded decompression (zip bomb) in DOCX/EPUB ingestion

**File:** `app/knowledge/extractors.py`, `app/config/settings.py`

`knowledge_max_file_size_mb` bounds the *compressed upload*, so it bounded
nothing about decompression. `zipfile.ZipFile.read()` inflated a member fully
into memory, and only afterwards did `ingestion.py` compare the resulting text
against `knowledge_max_chars` and reject it.

Reproduction at the baseline: a **0.195 MiB** DOCX upload (deflate compresses a
run of identical bytes roughly 1000:1) expanded `word/document.xml` to 200 MiB
and returned 209,715,200 characters after 1.14 s at **656.1 MiB peak heap** —
rejected only after paying the full cost. A single upload could exhaust the
container's memory.

EPUB was the same shape across every spine chapter, with no per-chapter or
cumulative bound. Neither extractor consulted `ZipInfo.file_size`,
`compress_size`, or the member count.

**Fix:**
- New settings `knowledge_max_uncompressed_mb` (200),
  `knowledge_max_zip_members` (2000), `knowledge_max_zip_ratio` (200).
- `_check_archive_bomb()` rejects using the ZIP central directory **before any
  member is decompressed**: aggregate declared uncompressed size, member count,
  and per-member compression ratio.
- `_read_zip_member()` enforces the limit *during* decompression, so a member
  that under-reports its size is still cut off.
- EPUB applies both a per-chapter limit and a cumulative budget.

### F-6 · High · PDF page limit enforced only after full parsing

**File:** `app/knowledge/extractors.py`

`knowledge_max_pages` was compared after every page had been extracted, so a
100k-page PDF paid the complete parse and memory cost before rejection.

**Fix:** read the page count from the already-parsed page tree and reject
before the extraction loop; also abort mid-loop once cumulative extracted text
passes `knowledge_max_chars`. A zero-page PDF is rejected explicitly.

### F-7 · High · Log redaction missed real provider secret formats

**File:** `app/security/redaction.py`

The original three patterns (Telegram literal, Telegram URL, labelled
`key=value`) missed every secret shape the audit probed. All of these leaked
into `logs/bot.log` verbatim:

- bare Google/Gemini key (`AIza…`)
- bare OpenAI (`sk-proj-…`) and Anthropic (`sk-ant-…`) keys
- PostgreSQL/MySQL/Mongo DSN with an inline password
- AWS access key ID (`AKIA…`)
- JWT
- `authorization: Bearer …`
- card-like digit runs
- long opaque Telegram `file_id` values (these grant file access)
- PEM private key blocks

**Fix:** rewrote `app/security/redaction.py` with provider-specific patterns
ordered widest-first (PEM → DSN → auth header → labelled pairs → provider
literals), so a structural pattern cannot be partially rewritten by a narrower
one. Already-redacted values are not re-matched.

### F-8 · High · Redaction formatter failed **open**

**File:** `app/security/redaction.py`

`RedactingFormatter.format()` caught the redaction error and then returned
`super().format(record)` — writing the **unredacted** record on exactly the
paths where redaction mattered most.

Two further defects: the formatter mutated `record.msg`/`record.args` in place,
so a second handler on the same logger saw an already-stripped record; and the
traceback that `Formatter.format()` appends is never part of `record.msg`, so a
secret raised inside a traceback was never redacted at all.

**Fix:** the formatter now copies the record, renders it, and redacts the
**fully rendered text** last. Any failure returns
`[REDACTION FAILED] record from <logger>` suppressed` rather than the raw text.

### F-9 · High · Log injection via embedded control characters

**File:** `app/security/redaction.py`

A multi-line payload was written as-is, so attacker-controlled text could forge
an additional log line with a fake level prefix (`"…\n2026-01-01 - app - INFO -
admin granted"`), defeating log review and any naive parser.

**Fix:** CR/LF are escaped to literal `\n`/`\r`, remaining C0/C1 control bytes
are escaped as `\xNN`, and text is NFKC-normalised.

### F-10 · High · `AUSTRO_LOG_LEVEL` was loaded and then ignored

**File:** `app/observability/logging_config.py`

The root logger was hardcoded to `logging.INFO`. An operator asking for
`WARNING` (e.g. to keep PII out of logs) still received INFO records.
Verified at the baseline: `settings.log_level == "WARNING"`, root effective
level `INFO`.

**Fix:** `_resolve_level()` maps the configured name to a level (default INFO),
and the root logger level is set from it. `setup_logging()` is now idempotent —
repeated calls previously stacked duplicate handlers and double every line.

### F-11 · High · Full user message text logged on every unhandled error

**File:** `app/telegram/main.py` (`error_handler`)

`logger.error(f"Update {update} …")` renders the whole `Update` object,
including `message.text` / `message.caption`, so any pasted document text the
user sent was persisted to `logs/bot.log` on every exception.

**Fix:** `_update_label()` builds an identifier from user id, username, chat id
and the update type only. An AST-based test asserts no logger call in
`app/telegram/main.py` interpolates a bare `update` or `message` object.

### F-12 · High · `pending` and `denied` memory injected into AI prompts

**File:** `app/memory/retrieval.py`

Retrieval loaded `memories.list(status="active")` and filtered nothing. A memory
still awaiting the user's confirmation (`pending`) and one the user had
explicitly **refused** (`denied`) were both injected into the prompt as if they
were established facts.

Reproduction at the baseline: three memories (`explicit`, `pending`, `denied`),
then a query matching all three — all three appeared in the rendered context
pack, including `"My salary is 900000 and I am unhappy at work."` and a health
diagnosis.

**Fix:** `CONSENT_ALLOWED_FOR_CONTEXT = {"automatic", "explicit"}` gates the
scoring loop, and an unrecognised consent value fails closed. An assertion
ties the allow-list to `CONSENT_STATES` so a newly added state cannot silently
become retrievable.

### F-13 · High · RAG evidence region had no closing delimiter

**File:** `app/knowledge/rag.py`, `app/knowledge/models.py`

Evidence blocks were concatenated into the prompt with no closing marker, so a
document containing `</evidence>`-style text had no reliable way to be
distinguished from the real end of the region. The system prompt described the
trust boundary but the prompt text did not enforce it.

**Fix:** evidence is wrapped in literal `<evidence>` … `</evidence>` tags, any
occurrence of those tags inside a snippet is neutralised, and two system-prompt
rules were added describing the delimiters and the required refusal to act on
instructions embedded in evidence.

### F-14 · High · No Docker build-context control

**Files:** `.dockerignore` (new), `Dockerfile`

No tracked `.dockerignore` existed while `Dockerfile` used `COPY . .`.
`.gitignore` has **no effect** on a Docker build context, so local `.env`
files, SQLite databases, `backups/*.sql` dumps (full memory and knowledge
content), logs, virtualenvs and `.git` were all sent to the daemon and became
image layers — readable by anyone who could pull the image, even if a later
layer deleted them.

**Fix:** added a strict `.dockerignore` (secrets, databases, backups, logs,
runtime state, VCS, build artefacts), and replaced `COPY . .` with an explicit
allow-list of runtime paths. `tests/test_docker_context.py` asserts that every
secret path is excluded, every runtime path is still included, `.gitignore`
secret rules are covered by `.dockerignore`, and that no `COPY` reintroduces
an excluded path. `Dockerfile.backup` already used an explicit `COPY scripts/`.

### F-15 · Medium · PostgreSQL transport encryption silently downgradable

**File:** `app/database/connection.py`, `app/config/settings.py`

No TLS arguments were passed, so libpq applied its own default of
`sslmode=prefer`: TLS was attempted, and a **silent fallback to plaintext was
accepted** on failure. `DB_PASSWORD` and every memory and knowledge row would
cross the network in the clear with no error and no log line.

**Fix:** new settings `db_sslmode`, `db_sslrootcert`, `db_sslcert`,
`db_sslkey`. `_postgres_ssl_kwargs()` passes them explicitly, and
`_assert_tls_for_remote_host()` **refuses to connect** to a non-loopback host
unless `sslmode` is `require` or `verify-full`. Loopback and the compose
service name `db` remain allowed; `DB_ALLOW_INSECURE=1` is an explicit,
never-default opt-out.

### F-16 · High · CI security step masked its own failure

**File:** `.github/workflows/ci.yml`

```yaml
run: python -m pytest tests/test_security.py -v || python -c "... skipping"; sys.exit(0)
```

`tests/test_security.py` does not exist in the tree, so the `||` branch always
ran and CI reported success for a suite that never executed. The secret-scan
action was also pinned to the moving ref `trufflesecurity/trufflehog@main`.

**Fix:** the step now lists the security test files that **do** exist, fails
loudly if any is missing, and runs pytest over them. TruffleHog is pinned to
`v3.97.9` (the published tags carry a leading `v`; an earlier `3.88.24` had no
such ref and failed the job at "Set up job" before any code ran — run #19).
A top-level `permissions: contents: read` was added; no job in the workflow
needs write access. `test_third_party_actions_are_pinned_to_a_release` now
enforces both rules.

### F-17 · High · Low-confidence memories were unreachable and permanently dead

**Files:** `app/memory/write_gate.py`, `app/memory/service.py`,
`app/memory/repositories.py`

Found while adding the F-12 consent gate, when
`tests/test_memory.py::test_retrieval_surfaces_low_confidence_only_on_overlap`
began failing.

`MemoryWriteGate.to_item` parks any `confidence == "low"` candidate at
`consent_state='pending'`. But `MemoryService.pending()` filters on **two**
conditions — the consent state *and* `metadata['needs_confirmation']` — and the
gate only wrote `metadata={"sensitive": ...}`. The result was a memory that:

- could not reach an AI prompt (correct, per F-12), **and**
- did not appear in the pending review menu, **and**
- could therefore never be confirmed to `consent_state='explicit'`.

So every low-confidence memory was written to the database and then became
permanently invisible and unusable — silently accumulating dead rows that the
user had no way to accept or reject. `_persist_conflict` had the same gap on
the conflict-resolution path.

**Fix:** `to_item` now sets `metadata["needs_confirmation"] = True` whenever it
assigns `consent_state='pending'`, and `MemoriesRepository.update()` gained an
optional `needs_confirmation` argument that `_persist_conflict` uses for the
same purpose (omitting it leaves the stored flag untouched, so a user edit does
not silently drop it). The pre-existing memory test was updated to state the
model correctly — the memory is parked, not "approved but flagged low" — and
now also asserts that it is invisible before approval and retrievable after.

---

## 4. Open items and residual risk

| # | Severity | Item | Status |
|---|---|---|---|
| R-1 | Medium | Knowledge ingestion runs blocking PDF/ZIP parsing on the event loop | Open — `TextExtractor` is CPU/IO-bound and is invoked from async handlers. Fixing it requires threading the ingestion pipeline; deferred to avoid changing concurrency semantics without load evidence. |
| R-2 | Medium | `scripts/pg_backup.py` writes unencrypted whole-database dumps | **Resolved** — AES-256-GCM encryption (`cryptography>=42`, 32-byte key from `BACKUP_ENCRYPTION_KEY_FILE`, fail-closed, stable versioned container, streaming, manifest SHA-256 of the original SQL) is implemented and verified by 16 tests in `tests/test_backup.py`. CI re-runs the container backup path. |
| R-3 | Medium | Ingestion failure state machine can wedge a source | Open — some parse/IO failure paths do not transition the source out of its in-progress state, so a retry may not be scheduled. |
| R-4 | Medium | Inconsistent owner_user_id scoping across some learning repositories | **Resolved** — see R-5. |
| R-5 | Low | Owner filters inconsistent across learning repositories | **Resolved** — `LearningObjectivesRepository.get()`, `list_for_goal()`, `set_status()`, `update_mastery()`, `set_prerequisites()` and `SessionsRepository.get()`, `set_step()`, `complete()` enforce `owner_user_id` at the SQL boundary; all callers pass the owner explicitly. Verified by 10 IDOR/cross-user tests in `tests/test_learning.py`. |

### Verification status

- **Local verification:** Complete. All 17 findings fixed, pyflakes clean,
  compileall clean, `pip check` clean, `pip-audit` clean, Bandit clean
  (fail-closed, exit 0), `scripts/verify.py` PASS, `smoke_test.py` PASSED,
  full pytest: **479 passed, 19 skipped, 0 failed**. The four upload/ingestion
  failures from run `37773381695` are fixed (§2a).
- **CI verification:** Complete. Run `37934832595` on `a218fd2` — **all 8 jobs
  success**: `test` (3.10/3.11/3.12/3.13/3.14, full `pytest -v` green on each),
  `Staging stack`, `dependency-audit`, `performance-tests`. Every step inside a
  `test` job ran (none skipped): Bandit, smoke, PostgreSQL integration
  (18/18 passed, 0 skipped), `verify.py`, security tests, TruffleHog secret scan.
  The previous run `37773381695` failure (4 upload tests) is resolved.
- **Bandit:** Installed locally (1.9.4) and passing with the CI flags.
- **Docker/PostgreSQL:** Not available locally; exercised in CI.

### Still not verified anywhere

- **Live Gemini / Telegram behaviour** — no real credentials were used or requested, by design.
- **Python 3.10/3.11/3.13+** — local run is 3.14 only; CI matrix covers the range.
- **Production/staging runtime** — no deployment was attempted.

---

## 5. Documentation reconciliation

Updated in this change set:

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | Rewritten with real newlines; Python matrix 3.10/3.11/3.12/3.13/3.14; Bandit step fail-closed (`--severity-level medium --confidence-level medium`, no `|| true`, no unsupported `-f sarif`); every step has `run:` or `uses:` |
| `pyproject.toml` | `requires-python = ">=3.10"` (consistent with CI matrix) |
| `requirements.txt` | Added `cryptography>=42.0.5,<51` (runtime AES-256-GCM backup encryption, needed in the container image) and `defusedxml>=0.7.1,<1` (hardened EPUB XML parsing) |
| `requirements-dev.txt` | Kept `cryptography` out (pulled in via `-r requirements.txt`); pinned `bandit>=1.7.5,<2` |
| `scripts/pg_backup.py` | Single `compress_archive()` definition; single `build_manifest()` definition; AES-256-GCM with 32-byte key; fail-closed encryption (raises if key missing when enabled); streaming/chunked processing; stable encrypted archive format `[0x01][nonce][ciphertext][tag]`; `verify_archive()` handles encrypted backups; `BACKUP_ENCRYPTION_DEFAULT_KEY` removed; verify/restore work encrypted |
| `scripts/pg_restore_drill.py` | Detects encrypted backup; streams through `open_archive_sql`; validates archive + content SHA-256; verifies manifest-vs-actual encryption; restores via `psql` stdin |
| `tests/test_backup.py` | 16 encryption tests (round-trip, tamper, wrong key, fail-closed missing/short key, manifest secrecy, key-file-only loading, versioned container, restore-drill source check) — 47 tests total |
| `tests/test_memory.py` | 5 duplicate `test_forget_redacts_versions_direct` removed; 1 strong regression test kept; added `test_forget`, `test_clear`, `test_hard_delete` |
| `tests/test_learning.py` | 10 IDOR/cross-user session tests (User A/User B) — 24 tests total |
| `app/learning/repositories.py` | `LearningObjectivesRepository.get()/list_for_goal()/set_status()/update_mastery()/set_prerequisites()` and `SessionsRepository.get()/set_step()/complete()` take `owner_user_id` and enforce it in SQL |
| `app/learning/session.py` | `SessionManager.complete()` now calls `self._store.sessions.complete(owner_user_id, session_id, result)` correctly |
| `app/knowledge/extractors.py` | Untrusted EPUB/container XML parsed with `defusedxml.ElementTree.fromstring` (XXE-safe) |
| `app/knowledge/chunker.py`, `app/memory/models.py` | SHA-1 (non-security dedup keys) marked `usedforsecurity=False` |
| `app/database/repositories.py` | `update_profile()` / `ProgressRepository.log()` validate `**kwargs` keys against column allowlists before interpolating |
| `app/knowledge/repositories.py`, `app/learning/repositories.py`, `app/memory/repositories.py` | B608 site-level hardenings: SQL built from literal fragments only, or allowlist-validated (`# nosec B608` with justification) |
| `tests/test_rate_limit_guards.py` | Upload `register_upload` double changed from sync `Mock` to `AsyncMock` (matches the real async API); added `test_allowed_upload_passes_source_id_to_background_ingestion`, `test_duplicate_upload_does_not_schedule_ingestion`, `test_upload_validation_error_aborts_with_message_only`, `test_upload_registration_error_aborts_without_scheduling` |
| `tests/test_rate_limit_integration.py` | Upload `register_upload` double changed from sync `Mock` to `AsyncMock` |
| `tests/test_learning.py`, `tests/test_repositories_pg.py` | Real `register_upload` calls now awaited (were treating the returned coroutine as a dict) |

Updated in this change set (upload async contract):

- The 4 previously-failing upload/ingestion tests now pass; full suite is
  479 passed / 19 skipped / 0 failed locally (§2).

Still outstanding (not changed here, listed so it is not lost):

- `RELEASE_CHECKLIST.md` items 10 and 11 remain open, as do the
  hosting-dependent items that were intentionally not attempted.
- Python support: `pyproject.toml` declares `requires-python = ">=3.10"` and CI
  exercises 3.10/3.11/3.12/3.13/3.14. The range is verified in CI.
- Production/staging runtime — no deployment was attempted.

---

## 6. Full validation

After fixes:

Run locally:

```
python -m compileall -q app main.py handlers.py config.py database.py reminder_scheduler.py redaction.py smoke_test.py scripts tests
python -m pyflakes main.py handlers.py config.py database.py reminder_scheduler.py redaction.py app tests scripts/staging_validate.py scripts/staging_rollback.py
python -m pip check
python -m pip_audit -r requirements.txt -r requirements-dev.txt
bandit -r app/ --severity-level medium --confidence-level medium
pytest
python scripts/verify.py
python smoke_test.py
```

All of the above were executed on this change set and pass (see §1/§2).

Run the complete regression suite (done: 479 passed, 19 skipped, 0 failed —
see §2a; 18 PG/performance tests skip locally, exercised in CI).

Then commit the fixes.

Do not hide failures.

---

## 7. Push and CI

Commit with a clear message, for example:

```
security: fix post-push audit regressions
```

Push normally:

```
git push origin main
```

Wait for GitHub Actions.

Verify every job:

- Python 3.10
- Python 3.11
- Python 3.12
- Python 3.13
- Python 3.14
- Bandit
- full pytest
- PostgreSQL integration
- dependency audit
- Docker/staging
- backup validation
- performance
- secret scan
- security tests

If any job fails: STOP and report the exact failure.

Do not weaken tests or CI to obtain green status.

---

## 8. Final verification required

The audit can only be called remotely verified after:

1. the corrected commit reaches GitHub ✓ (baseline `c8066f4`, corrections
   `f8c29ae`, `08e923a`, and upload-contract fix `a218fd2` are all published)
2. the CI workflow parses correctly ✓ (rewritten YAML with real newlines,
   validated with `yaml.safe_load`; every step has `run:`/`uses:`; the run
   spawned all defined jobs, not zero)
3. all required jobs actually run ✓ — run `37934832595` (on `a218fd2`)
   executed every job and every step (none skipped), including Bandit, smoke,
   PostgreSQL integration, `verify.py`, security tests and the secret scan
4. all required jobs pass ✓ — run `37934832595`: all 8 jobs success
   (`test` 3.10–3.14, `Staging stack`, `dependency-audit`, `performance-tests`);
   the full `pytest -v` is green on all five Python versions, so the four
   upload tests that failed in run `37773381695` now pass in CI
5. R-5 IDOR tests pass ✓ (run `37934832595` — verified locally too;
   owner_user_id enforced at SQL boundary)
6. backup encryption + restore verification pass ✓ (run `37934832595` —
   verified locally by 47 tests)
7. no new High/Critical findings remain ✓ — Bandit exit 0 (`No issues
   identified`) and `pip-audit` `No known vulnerabilities found` in the same
   run; no findings were introduced by this pass
8. CODE_AUDIT_REPORT.md matches the actual state ✓ (updated with the CI
   evidence above)

Return the final commit SHA, CI run URL, every job status, and all remaining risks.