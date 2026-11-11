# AUSTRO AI - Privacy & Retention (Phase F)

This document describes the privacy guarantees, data retention rules, and deletion commitments for the AUSTRO AI system.

## 1. What AUSTRO Stores

### 1.1 User-Provided Data
- **Goals:** User-defined personal development goals (text description, target dates)
- **Habits:** User-defined habits with tracking (name, frequency, period)
- **Memories:** User-specific memories with content, type, scope, and confidence
- **Knowledge Sources:** Uploaded books, notes, PDFs, documents with associated metadata
- **Learning Data:** Learning sessions, assessments, misconceptions, progress tracking
- **Coach Data:** Coach logs, focus areas, blockers, smallest actions

### 1.2 Derived Data
- **Embeddings:** Vector representations of knowledge documents and queries (for RAG)
- **Chunks:** Text chunks extracted from uploaded documents
- **Sections:** Document sections with page offsets
- **Citations:** Source attribution for retrieved knowledge
- **Progress Models:** Computed learning progress and mastery states
- **Schedules:** Spaced repetition review schedules
- **Daily/Weekly Reviews:** Generated review content

### 1.3 What AUSTRO Does NOT Store
- **Raw LLM Prompts:** User messages and AI prompts are NOT persisted to the database.
- **LLM Responses:** AI-generated responses are NOT stored in the database (see telemetry below).
- **API Keys/Secrets:** Bot token, OpenRouter key, or any secrets are never stored in database records.
- **Conversation Text:** Full conversation history is not stored as a monolithic blob; only audited events are logged.
- **Knowledge search queries:** the text a user types into a knowledge search is NOT stored. Each
  search writes a `knowledge_retrieval_events` row with the counts, latency, generator and cited chunks,
  and `query` is written as an empty string (`KnowledgeEventRepository.log`). Verified by
  `tests/test_retrieval_query_privacy.py` (SQLite) and `_pg.py` (PostgreSQL). Rows written by earlier
  builds may still hold query text; `scrub_legacy_query_text()` blanks it and is run only deliberately.

## 2. Retention Rules

### 2.1 Per-User Control
Most data is retained until the user explicitly deletes it.

| Data Type | Retention | User Control |
|-----------|-----------|--------------|
| Goals | Until deleted | `/delete` erases the account and every derived row (§3.1). |
| Habits | Until deleted | `/delete` erases the account and every derived row (§3.1). |
| Memories | Until deleted | Coach menu: view, search, edit, forget by ID, clear all, export as JSON. `/delete` also erases them. |
| Knowledge Sources | Until deleted | Each source has a 🗑 button in "كتبي" (`kdel:<id>`) that removes the row **and** the stored file; `/delete` erases everything. |
| Learning Sessions | Until deleted | `/delete` erases all learning tables. |
| Coach Logs | 90 days | Written by `coach_analysis` (`app/telegram/handlers.py`); enforced by the daily `retention_purge` job (`COACH_LOG_RETENTION_DAYS`). Included in `/export`. |
| Activity Log | 90 days | Written by `ActivityRepository.log`; enforced by the daily `retention_purge` job (`ACTIVITY_LOG_RETENTION_DAYS`); included in `/export`. |
| Embeddings/Chunks | Per knowledge source | Removed when the source is deleted (🗑 button or `/delete`). |

### 2.2 System-Generated Retention
- **Activity Log:** 90-day retention, enforced daily by `retention_purge` (03:10 UTC) via `PrivacyRepository.purge_expired_logs`.
- **Coach Logs:** Written by `coach_analysis` (`app/telegram/handlers.py`) for each performance analysis; 90-day retention, same job; `COACH_LOG_RETENTION_DAYS=0` disables it.
- **Embedding Cache:** Cleared on knowledge source re-upload (dedup via checksum).

### 2.3 Default Retention Settings
These values are read from the environment at startup and enforced by the daily job:
`ACTIVITY_LOG_RETENTION_DAYS` (default 90) and `COACH_LOG_RETENTION_DAYS` (default 90).
A value of `0` disables the purge for that table. Tests: `tests/test_privacy_account.py`.

## 3. Deletion Rules

### 3.1 User-Initiated Deletion
**Implementation status (verified against the registered Telegram handlers):**

- **`/export`** (registered): sends a JSON document containing every row the caller owns across the
  core, learning, memory and knowledge tables (`PrivacyService.export`). Scoped by owner id, so one
  user's export can never contain another user's rows.
- **`/delete`** (registered): shows what would be erased and asks for an explicit confirmation button.
  The command alone destroys nothing. The confirmation carries a one-time token, bound to the user id
  and valid for 10 minutes; a replayed, stale or foreign tap is refused. On confirmation,
  `PrivacyRepository.delete_all` erases every user-owned row in **one transaction** and the reply
  reports only what a re-read afterwards proved (`DeletionResult.verified`). A failed transaction
  deletes nothing and says so.
- **Knowledge sources:** the "كتبي" list renders one 🗑 button per source (`kdel:<id>`), which removes
  the row and the stored file.
- **Memory (coach menu):** view, search, edit, forget by ID, clear all, export as JSON.

There is deliberately **no `/forget`**. Its intended effect (clear learning state) is already covered
by `/delete`, and a partial "clear learning" command would leave the core tables behind while looking
like an erasure.

| Command | Effect | Status |
|---------|--------|--------|
| `/export` | Complete JSON snapshot of the caller's data | **Implemented** (`app/telegram/main.py`, `app/application/privacy.py`) |
| `/delete` | Full account deletion: users row and all associated data, confirmed | **Implemented**, confirmed, transactional, verified |
| `kdel:<id>` | Delete one knowledge source + its stored file | **Implemented** |

**Memory erasure, as implemented:** the coach menu's forget (by ID) and clear (all) actions call the
memory erase path (`MemoriesRepository.erase`, `app/memory/repositories.py`). For the scrub, tombstone
and atomicity details, see `MEMORY_ARCHITECTURE.md` §4.

### 3.2 Deletion Cascade Order (executed by `/delete`)
`PrivacyRepository.delete_all` runs one transaction, children before parents:
1. **knowledge** - citations (scoped through `knowledge_retrieval_events`), chunks, embeddings,
   sections, versions, documents, retrieval events, claims, collection links, storage files, sources,
   collections
2. **memory** - access log, events, versions, prefs, memories
3. **learning** - events, progress, plans, reviews, assessments, misconceptions, mastery, sessions,
   lessons, curricula, objectives, goals
4. **core** - habit logs, habits, plans, progress, reviews, study sessions, language/programming/
   security progress, reminders, coach logs, activity log, goals
5. **users** row, last

The table lists are module-level literals and a test asserts that *every* table carrying
`user_id`/`owner_user_id` is covered, so a future table cannot be silently retained.

### 3.3 Admin-Initiated Deletion
- Not implemented: admins cannot trigger deletion on behalf of a user. No such command exists (see 3.1).
- Planned: the same cascade order as 3.2, with consent verification.
- Deletion leaves no per-user trace to log it against (the row is gone); the event is logged server-side by `PrivacyService`.

### 3.4 Irreversibility
- Deletion **is** available (`/delete`) and is irreversible; the confirmation step exists and is
  enforced by a token-bound button.
- Backup restoration may recover data if backups exist (see `SECURITY_ARCHITECTURE.md` backup procedures). This applies to any data, whether or not it was later deleted.
- Implemented: `/delete` warns, lists what will be removed, and requires a second, explicit tap.

## 4. Data Derivation & Sharing

### 4.1 What Is Derived (Not Stored Raw)
- Embeddings are derived from knowledge document chunks (stored alongside, not instead of, the original text).
- Progress models are derived from assessment answers and session completion.
- Mastery states are derived from the mastery state machine (NEW→LEARNING→PRACTICING→NEAR_MASTERY→MASTERED→REGRESSED).
- Misconception patterns are derived from incorrect assessment answers.

### 4.2 What Is Never Shared Globally
- **No Global Learning:** User data is never used to train or improve a global/shared LLM.
- **No Cross-User Aggregation:** Derived insights from one user are never applied to another user.
- **No Public Benchmarks:** Individual user progress, mastery levels, or assessment results are never shared or published.

### 4.3 What Is Shared (Explicitly)
- **Export:** `/export` sends a complete JSON snapshot of the caller's data. The coach menu still exports memories on their own.
- **Coach Context:** Coach data is shared only within the user's own session (isolated per user).
- **Knowledge Sources:** Uploaded documents are per-user owned; re-uploads are idempotent via `UNIQUE(owner_user_id, checksum)`.

## 5. Privacy Guarantees

### 5.1 Data Isolation
- **Owner Scoping:** Every database query filters by `owner_user_id`. There are no global/shared tables across users.
- **Cross-User Attack Resistance:** Verified that User A cannot access User B's goals, habits, memories, knowledge sources, or learning history.

### 5.2 Sensitive Data Protection
- **Passwords/SSNs/Bank Accounts:** Detected via regex in the memory write gate; rejected or parked pending explicit user consent.
- **Redaction in Logging:** All logging goes through `RedactingFormatter` to scrub potential secrets.
- **No Secrets in Prompts:** System prompts never include user data, API keys, or authentication tokens.

### 5.3 Telemetry Privacy
- **No Prompt/Response Storage:** `AITelemetry.record()` stores only metadata (run_id, capability, provider, model, latency, success, retry_count, error, tokens, cost) and emits the same metadata as one JSON log line. Prompts and responses are never included. Token/cost values are `null` when the provider does not report them.
- **No Secret Inclusion:** API keys, bot tokens, or user-identifying data are never included in telemetry records.
- **Ring Buffer:** Telemetry uses a bounded ring buffer (max 200 records) to prevent unbounded growth.

### 5.4 File Privacy
- **Checksum Verification:** SHA-256 checksums verify file integrity on read; duplicates are skipped.
- **Path Traversal Protection:** Uploaded files cannot escape the designated `knowledge_storage/` directory.
- **MIME-Type Validation:** Only accepted formats (PDF, DOCX, EPUB, TXT, MD) are processed; others are rejected.

## 6. Retention Summary Table

| Category | Data | Retention | Deletion Method (current) |
|----------|------|-----------|---------------------------|
| User Goals | Goals | Until deleted | `/delete` (confirmed, transactional, verified) |
| User Habits | Habits | Until deleted | `/delete` (confirmed, transactional, verified) |
| User Memories | Memories | Until deleted | Coach menu: forget by ID or clear all; `/delete` erases all |
| Knowledge Sources | Documents, chunks, embeddings | Until deleted | Per-source button in "كتبي" (`kdel:<id>`) removes row + file; `/delete` erases all |
| Learning Data | Sessions, assessments, misconceptions | Until deleted | `/delete` erases every learning table |
| Coach Data | Logs, focus, blockers | 90 days | Daily `retention_purge`; `/export` includes them |
| Activity Log | Audit events | 90 days | Written by `ActivityRepository.log`; daily `retention_purge`; `/export` includes it |
| Telemetry | Run metadata | Ring buffer (200 max) | Automatic (oldest purged) |
| User Identity | Telegram user ID | Until account deleted | `/delete` removes the `users` row last |

## 7. Compliance

### 7.1 GDPR Rights
These are the current capabilities. The GDPR rights are not fully met by the current application.
- **Right to Access:** Available. `/export` returns a complete JSON snapshot of every row the caller owns (core, learning, memory and knowledge tables), scoped to that user.
- **Right to Rectification:** Partially available. Memories can be edited through the coach menu. Other data editing paths were not re-verified for this document.
- **Right to Erasure:** Available. `/delete` erases the account and every derived row in one transaction, after an explicit confirmation; knowledge sources can also be deleted individually (🗑 in "كتبي"). See §3.1.
- **Right to Data Portability:** Not available as a complete machine-readable snapshot. Memory-only JSON export exists.

### 7.2 Data Protection Principles
- **Purpose Limitation:** Data is used only for the user's personal development coaching and learning.
- **Data Minimization:** Only data necessary for coaching/learning is collected; no extraneous fields.
- **Accuracy:** User can review and correct their data at any time.
- **Storage Limitation:** Retention is enforced for `activity_log` and `coach_logs` (90 days, daily job, §2.2). User-owned content is retained until the user deletes it.
- **Integrity:** Checksums verify data integrity; duplicates are prevented.
- **Confidentiality:** Data is isolated per user; no cross-user leakage.
- **Accountability:** `activity_log` is written by `ActivityRepository.log`, retained 90 days, and purged by the daily job (§2.2).

---
# File: PRIVACY_RETENTION.md

Created as part of Phase F privacy and retention documentation.