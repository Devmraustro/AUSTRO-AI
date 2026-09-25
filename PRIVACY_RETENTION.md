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
- **API Keys/Secrets:** Bot token, Gemini key, or any secrets are never stored in database records.
- **Conversation Text:** Full conversation history is not stored as a monolithic blob; only audited events are logged.

## 2. Retention Rules

### 2.1 Per-User Control
Most data is retained until the user explicitly deletes it.

| Data Type | Retention | User Control |
|-----------|-----------|--------------|
| Goals | Indefinite | View, edit, delete via `/forget` or `/delete` |
| Habits | Indefinite | View, edit, delete via `/forget` or `/delete` |
| Memories | Indefinite | View, edit, forget, clear via coach menu |
| Knowledge Sources | Indefinite | View, delete, re-upload (idempotent) |
| Learning Sessions | Until course completion | Auto-clear on `/forget` |
| Coach Logs | 90 days (configurable) | Visible in coach menu, exportable |
| Activity Log | 90 days (configurable) | Audit trail, exportable |
| Embeddings/Chunks | Indefinite (per knowledge source) | Depends on knowledge source deletion |

### 2.2 System-Generated Retention
- **Activity Log:** Retained for 90 days for audit purposes (configurable via `ACTIVITY_LOG_RETENTION_DAYS`).
- **Coach Logs:** Retained for 90 days (configurable via `COACH_LOG_RETENTION_DAYS`).
- **Embedding Cache:** Cleared on knowledge source re-upload (dedup via checksum).

### 2.3 Default Retention Settings
```python
# Configurable in settings.py
ACTIVITY_LOG_RETENTION_DAYS = 90
COACH_LOG_RETENTION_DAYS = 90
```

## 3. Deletion Rules

### 3.1 User-Initiated Deletion
Users can delete their data via Telegram commands:

| Command | What It Deletes | Cascade |
|---------|----------------|---------|
| `/forget` | Clears learning state: sessions, assessments, misconceptions, progress models | User data only; no cascade to other users |
| `/delete` | Full account deletion: users row + all associated data | Cascades to: goals, habits, memories, knowledge sources, learning sessions, coach logs, activity log |
| `/export` | Exports complete user data snapshot | Read-only; does not delete |

### 3.2 Deletion Cascade Order (per schema_migrations)
1. **knowledge** (v1) - knowledge sources and all derived data
2. **memory** (v1) - memories, memory versions, memory events, memory access log
3. **learning** (v1) - learning goals, objectives, curricula, lessons, sessions, assessments, misconceptions, progress, reviews, events

### 3.3 Admin-Initiated Deletion
- Admins can trigger `/delete` on behalf of a user (with user consent verification).
- Admin-initiated deletion follows the same cascade order.
- Deletion events are logged to `activity_log` with the admin user ID and target user ID.

### 3.4 Irreversibility
- Once `/delete` is confirmed, data recovery is not possible through the normal interface.
- Backup restoration may recover data if backups exist (see `SECURITY_ARCHITECTURE.md` backup procedures).
- Users are warned before `/delete` confirmation.

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
- **Export:** User-initiated data export provides a complete snapshot for the user's personal use.
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
- **No Prompt/Response Storage:** `AITelemetry.record()` stores only metadata (run_id, capability, provider, model, latency, success, retry_count, error, tokens, cost). Prompts and responses are never included.
- **No Secret Inclusion:** API keys, bot tokens, or user-identifying data are never included in telemetry records.
- **Ring Buffer:** Telemetry uses a bounded ring buffer (max 200 records) to prevent unbounded growth.

### 5.4 File Privacy
- **Checksum Verification:** SHA-256 checksums verify file integrity on read; duplicates are skipped.
- **Path Traversal Protection:** Uploaded files cannot escape the designated `knowledge_storage/` directory.
- **MIME-Type Validation:** Only accepted formats (PDF, DOCX, EPUB, TXT, MD) are processed; others are rejected.

## 6. Retention Summary Table

| Category | Data | Retention | Deletion Method |
|----------|------|-----------|-----------------|
| User Goals | Goals | Until deleted | `/forget` or `/delete` |
| User Habits | Habits | Until deleted | `/forget` or `/delete` |
| User Memories | Memories | Until deleted | Coach menu / `/forget` or `/delete` |
| Knowledge Sources | Documents, chunks, embeddings | Until deleted | `/delete` or re-upload (idempotent) |
| Learning Data | Sessions, assessments, misconceptions | Until deleted | `/forget` or `/delete` |
| Coach Data | Logs, focus, blockers | 90 days (configurable) | Export or `/delete` |
| Activity Log | Audit events | 90 days (configurable) | Automatic purge / `/delete` |
| Telemetry | Run metadata | Ring buffer (200 max) | Automatic (oldest purged) |
| User Identity | Telegram user ID | Until account deleted | `/delete` |

## 7. Compliance

### 7.1 GDPR Rights
- **Right to Access:** `/export` provides complete user data export.
- **Right to Rectification:** User can edit goals, habits, and other data through the UI.
- **Right to Erasure:** `/delete` performs full account erasure per GDPR requirements.
- **Right to Data Portability:** Export provides machine-readable data snapshot.

### 7.2 Data Protection Principles
- **Purpose Limitation:** Data is used only for the user's personal development coaching and learning.
- **Data Minimization:** Only data necessary for coaching/learning is collected; no extraneous fields.
- **Accuracy:** User can review and correct their data at any time.
- **Storage Limitation:** Retention rules are documented and enforced (90-day default for logs).
- **Integrity:** Checksums verify data integrity; duplicates are prevented.
- **Confidentiality:** Data is isolated per user; no cross-user leakage.
- **Accountability:** All data access/ modification events are logged to `activity_log`.

---
# File: PRIVACY_RETENTION.md

Created as part of Phase F privacy and retention documentation.