# AUSTRO AI - Telegram Security Architecture (Phase F)

This document describes the security threat model and mitigations for the Telegram bot integration.

## 1. Callback Validation

### 1.1 Telegram Callback Queries
All Telegram callback queries must be validated as originating from Telegram.

**Mitigations:**
- **Secret Token Verification:** Each callback query includes a `hash` computed from the query ID, message ID, and bot token. The server recomputes this hash and rejects requests where it doesn't match.
- **Origin Authentication:** The `python-telegram-bot` library's `CallbackQueryHandler` ensures updates come from Telegram's servers.
- **Replay Prevention:** Callback data includes timestamps or session identifiers to prevent replay attacks.

### 1.2 Deep-Link Authorization
Deep links (e.g., `/start <referral_id>`) must be validated against the bot's configuration.

**Mitigations:**
- **Referral ID Bounds:** Referral IDs are validated as integers within expected ranges.
- **User Identity Binding:** Deep-link parameters are bound to the user session and cannot be spoofed to affect other users.
- **No Secrets in Deep Links:** Referral IDs and session tokens do not contain API keys or secrets.

## 2. Conversation State Isolation

### 2.1 Per-User State
The conversation state is strictly per-user. No global/shared conversation state exists.

**Mitigations:**
- **Persistence via Database:** Conversation state (onboarding steps, selected options, pending actions) is stored in the user's database row, not in memory or Redis shared across users.
- **Database Filtering:** All conversation state queries filter by `user_id` or `owner_user_id`.
- **No Cross-User State Leakage:** User A's onboarding progress, selected goals, or pending actions cannot be accessed via User B's session.

### 2.2 State Storage
- **Onboarding:** `user_onboarding` table tracks steps per user
- **Pending Actions:** `coach_logs` and `activity_log` store per-user events
- **Session State:** Learning session state stored in `learning_sessions` with `owner_user_id` filtering

## 3. Command Authorization

### 3.1 /start Command
The `/start` command is the entry point and initializes user onboarding.

**Mitigations:**
- **New User Registration:** `/start` creates a new user row if one does not exist, or resumes existing onboarding.
- **Parameter Validation:** Deep-link parameters (e.g., referral codes) are validated and bounded.
- **No Admin-Only Restrictions at /start:** All users can start the bot.

### 3.2 Admin-Only Commands
Commands like `/forget`, `/export`, `/delete`, `/admin*` are restricted to authorized admins.

**Mitigations:**
- **Telegram User ID Check:** Each command handler checks `update.effective_user.id` against the configured admin list.
- **Default Admin List:** Admins are configured via `ADMIN_USER_IDS` in settings (empty by default, meaning no admin commands are active unless explicitly configured).
- **Fallback Response:** Non-admin users receive a "command not available" message when attempting admin commands.

### 3.3 /forget Command
Allows users to clear their learning state and memory.

**Mitigations:**
- **User-Scope Only:** `/forget` only affects the invoking user's data.
- **Audit Log:** `/forget` events are logged to `memory_events` and `activity_log` with the user ID.
- **Confirmation Required:** The command may require a confirmation step before irreversible data deletion.

### 3.4 /export and /delete
Users can export their data or request account deletion.

**Mitigations:**
- **User-Scope Only:** Export and deletion only affect the invoking user's data.
- **GDPR Compliance:** Export provides a complete snapshot of user data (goals, habits, memories, knowledge sources, learning progress).
- **Deletion Cascade:** Deleting a user cascades to associated data (sessions, assessments, misconceptions) per the schema migration order.
- **Export Log:** Export events are logged to `activity_log`.

## 4. File Upload Handling

### 4.1 Size Limits
- **Maximum File Size:** 50 MB per upload.
- **Enforcement:** `python-telegram-bot` `FileHandler` with `max_length` parameter.
- **Rejection:** Files exceeding the limit receive a "file too large" message.

### 4.2 Type Validation
- **Accepted Formats:** PDF, DOCX, EPUB, TXT, MD.
- **Rejection:** Unknown file types receive "unsupported format" message.
- **MIME-Type Check:** Content type is verified against accepted types.

### 4.3 Path Traversal Protection
- **_resolve Function:** All file paths go through a `_resolve()` guard that prevents `../` traversal.
- **Sanitized Filenames:** Uploaded filenames have path separators and dangerous characters stripped.
- **Storage Directory:** Files are stored within a dedicated `knowledge_storage/` directory with no user-controlled path components.

### 4.4 Virus/Malware Scanning (Conceptual)
- Uploaded files are processed for text extraction only; no code execution.
- PDF: `pypdf` extracts text+page offsets.
- DOCX/EPUB: stdlib `zipfile` + `xml.etree` extracts text.
- TXT/MD: Built-in read, always data.

## 5. Message Size Handling

### 5.1 Telegram Message Limits
- **Telegram Limit:** 4096 characters per message text.
- **AUSTRO Guard:** Output is truncated at 4090 characters with "... [truncated]" appended.
- **Long Output Splitting:** Multi-message output is used when exceeding the limit, with sequential numbering.

### 5.2 Flood/Spam Protection
- **Rate Limiting:** Per-user command rate limits prevent flood/spam.
- **Anti-Flood Delay:** Minimum 0.5s between command executions per user (configurable via `AI_MIN_REQUEST_INTERVAL`).
- **Callback Spam:** Callback query rate is limited to prevent rapid repeated interactions.

## 6. User Identity Handling

### 6.1 Telegram User Identity
- **Source of Truth:** `update.effective_user.id` is the authoritative user identifier.
- **Profile Data:** First name, last name, and username are captured on `/start` but are optional.
- **Never Trust Client-Supplied IDs:** The bot never uses user IDs provided in callback data or message content as authoritative.

### 6.2 Anonymous Users
- Users without a Telegram account identity (bots, service accounts) are rejected at `/start`.
- The bot token must be a user-type bot (not a group/bot-only token) for per-user state to work.

## 7. Admin-Only Actions

### 7.1 Admin Command Filter
All potentially destructive or admin-only actions check authorization.

**Implementation:**
```python
# Pseudocode pattern in all command handlers
ADMIN_USER_IDS = settings.ADMIN_USER_IDS or []
if ADMIN_USER_IDS and update.effective_user.id not in ADMIN_USER_IDS:
    await update.message.reply_text("This command is not available.")
    return
```

### 7.2 Admin-Protected Actions
- `/forget` - user state clearance
- `/export` - data export
- `/delete` - account deletion
- Any database modification commands

## 8. Summary of Security Controls

| Control | Implementation |
|---------|---------------|
| Callback validation | Telegram hash verification |
| Conversation state isolation | Database per-user filtering |
| Command authorization | Admin user ID check |
| File upload size | 50 MB max |
| File upload type | PDF, DOCX, EPUB, TXT, MD only |
| Path traversal protection | _resolve() guard |
| Message size | 4096 Telegram limit, 4090 AUSTRO guard |
| Flood/spam protection | Per-user rate limits |
| User identity | update.effective_user.id only |
| Admin actions | ID check against ADMIN_USER_IDS |

---
# File: TELEGRAM_SECURITY.md

Created as part of Phase F security documentation.