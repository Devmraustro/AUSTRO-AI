# AUSTRO AI - Security Incident Response (Phase F)

## Overview

This document contains operational runbooks for security incidents affecting the AUSTRO AI system. Each runbook follows a standardized format: DETECT → CONTAIN → REVOKE/ROTATE → INVESTIGATE → RECOVER → VERIFY → POST-INCIDENT REVIEW.

Incidents are classified by severity:
- **CRITICAL**: Immediate threat to user data or system integrity
- **HIGH**: Significant operational impact
- **MEDIUM**: Requires investigation and remediation
- **LOW**: Informational, no immediate action required

---

## 1. Telegram BOT_TOKEN Compromise

### Detection
- Unexpected bot behavior (posting messages not initiated by the system)
- Alerts from BotFather about unusual activity
- Failed login attempts in Telegram API logs
- User reports of unexpected bot behavior

### Containment
1. Immediately revoke the current bot token via BotFather
2. Set `BOT_TOKEN` environment variable to empty/unset
3. Enable read-only mode if possible
4. Isolate the bot process to prevent further message sending
5. Notify team members with token rotation authority

### Revoke/Rotate
1. Generate a new bot token via @BotFather
2. Rotate the compromised token immediately (do not reuse)
3. Update all references to the old token in configuration, code, and logs
4. Rotate any associated webhook secrets

### Investigate
1. Review Telegram API logs for the period of compromise
2. Identify what messages were sent, to whom, and when
3. Check for any unauthorized command executions
4. Determine the scope of data exposure
5. Preserve all logs and evidence

### Recover
1. Deploy the new bot token
2. Resume normal bot operations
3. Monitor for any recurrence of unusual activity
4. Verify all user-facing functions work correctly

### Verify
1. Test /start command functionality
2. Test key commands (/forget, /export, /goals, etc.)
3. Verify conversation state isolation still works
4. Confirm no user data was leaked during the compromise period

### Post-Incident Review
1. Document the timeline of the incident
2. Identify the root cause (phishing, compromised device, etc.)
3. Update security procedures to prevent recurrence
4. Review token storage and access patterns
5. Schedule follow-up security review

---

## 2. AI Provider Key Compromise (Gemini API Key)

### Detection
- Unusual API request patterns in Google Cloud Console
- Unexpected charges or quota exhaustion
- Alerts from Gemini API about anomalous usage
- Reports of strange AI output behavior

### Containment
1. Immediately revoke the current API key
2. Set `GEMINI_API_KEY` environment variable to empty
3. System falls back to local AI mode (use_local_fallback=True)
4. Isolate affected services from AI gateway

### Revoke/Rotate
1. Generate a new API key in Google AI Studio
2. Rotate the compromised key immediately
3. Update all configuration and code references
4. Rotate any API gateway secrets or downstream tokens

### Investigate
1. Review API usage logs for the compromise period
2. Identify what requests were made, by whom, and with what parameters
3. Check for any unauthorized model modifications
4. Determine what data was sent to the AI provider
5. Preserve all logs and API request/response evidence

### Recover
1. Deploy the new API key
2. Re-enable Gemini API if desired (set `use_local_fallback=False`)
3. Monitor API usage for anomalous patterns
4. Test all AI-dependent functionality

### Verify
1. Test all LLM-dependent features work with new key
2. Verify local fallback still works if Gemini key is unavailable
3. Test assessment grading, lesson generation, coach advice
4. Confirm no user data was exposed in AI prompts

### Post-Incident Review
1. Document the timeline and scope of the incident
2. Evaluate the effectiveness of the fallback to local AI
3. Review API key management practices
4. Implement additional monitoring if needed
5. Schedule credential rotation on regular schedule

---

## 3. Sensitive Data Exposure

### Detection
- User reports of data leakage in AI responses
- Log files containing passwords, SSNs, or account numbers
- Alerts from redaction/ filtering systems
- Security scan detecting secrets in logs

### Containment
1. Immediate redaction of affected log files or outputs
2. Disable any code paths that may expose sensitive data
3. Reset affected user passwords/credentials if stored
4. Isolate the affected component

### Revoke/Rotate
1. If passwords/SSNs were stored, force password reset for affected users
2. Rotate any exposed API keys or tokens
3. Update redaction rules if patterns were missed

### Investigate
1. Trace the data flow to find where sensitive data entered the system
2. Check the memory write gate for sensitive topic bypass
3. Review the redaction/formatting logic
4. Check for any path that bypasses the memory write gate validation
5. Identify what data types were exposed and to whom

### Recover
1. Update redaction rules with any newly discovered patterns
2. Reset affected user credentials
3. Re-enable affected system components
4. Run redaction validation tests

### Verify
1. Test that passwords, SSNs, and bank accounts are properly redacted
2. Test the memory write gate with sensitive topics
3. Verify no secrets appear in AI prompts or outputs
4. Run the sensitive data protection test suite

### Post-Incident Review
1. Document what sensitive data types were exposed
2. Improve regex patterns for sensitive topic detection
3. Review the memory write gate validation logic
4. Add additional redaction rules if gaps were found
5. Schedule periodic redaction audits

---

## 4. Cross-User Data Isolation Failure

### Detection
- User A can access User B's goals, habits, or memories
- Alerts from access control checks
- User reports of data belonging to other users visible in their interface
- Automated cross-user attack tests failing

### Containment
1. Immediately restrict the affected API endpoint or feature
2. Apply owner_user_id filtering at the database query level
3. Revoke access for the affected user(s)
4. Notify users whose data may have been exposed

### Revoke/Rotate
1. No credential rotation needed (this is an access control issue)
2. Reset session tokens for affected users
3. Review and fix any privilege escalation vulnerabilities

### Investigate
1. Trace the data access path to find the isolation bypass
2. Check database queries for missing owner_user_id filtering
3. Review the handler authorization logic
4. Check for any path traversal or parameter manipulation
5. Identify what data was accessible without authorization

### Recover
1. Fix the isolation bypass (add owner_user_id filtering)
2. Restore affected user access
3. Run cross-user isolation tests
4. Verify no other data paths have similar issues

### Verify
1. Run cross-user attack tests (User A cannot access User B's data)
2. Verify all data operations filter by owner_user_id
3. Test goals, habits, memories, knowledge sources, learning history
4. Verify flashcards, projects, exports, files are all user-scoped

### Post-Incident Review
1. Document the isolation bypass and root cause
2. Review all database query patterns for missing scoping
3. Add automated cross-user isolation tests to the regression suite
4. Review handler authorization logic
5. Schedule security review focus on data isolation

---

## 5. Prompt Injection Incident

### Detection
- AI output containing unexpected instructions or payloads
- User reports of bot behaving strangely
- Automated prompt injection test cases failing
- Unusual AI responses to normal queries

### Containment
1. Immediately switch to local AI fallback (`use_local_fallback=True`)
2. Remove any user-controlled content from system prompts
3. Review and validate all retrieved document content
4. Disable any features that pass user content to the LLM

### Revoke/Rotate
1. No credentials to rotate (prompt injection is a content issue)
2. Reset any user preferences that may have been manipulated
3. Update prompt injection test cases if new patterns discovered

### Investigate
1. Trace the injection path (uploaded document, memory, user message, metadata)
2. Check the RAG grounded answer system for trust separation
3. Review the memory write gate for any content acceptance bypasses
4. Check for any document processing that didn't separate data from instructions
5. Identify what injection pattern was used and where it failed

### Recover
1. Update prompt injection test cases with new patterns
2. Reset any manipulated user states or preferences
3. Re-enable affected features with improved validation
4. Test all injection test cases

### Verify
1. Run all prompt injection test cases (user message, book, metadata, memory, context)
2. Verify grounded answers use evidence markers [1], [2]... only
3. Verify no instructions from documents override system policy
4. Verify citations are always present and correct

### Post-Incident Review
1. Document the injection pattern and entry point
2. Improve data/instruction boundary enforcement
3. Review the trust-separated system prompt
4. Add additional prompt injection test cases
5. Schedule red team testing

---

## 6. Memory Poisoning

### Detection
- User reports of incorrect memories or goals
- Assessment answers creating false memories
- Memory events showing unexpected data
- Mastery state machine in unexpected state

### Containment
1. Review pending memories in the coach menu
2. Reset affected memory entries
3. Isolate the memory write gate from further input
4. Audit the memory events log

### Revoke/Rotate
1. No credentials to rotate
2. Acknowledge or retract poisoned memories via the coach interface
3. Reset mastery state if compromised

### Investigate
1. Trace the memory creation path (assessment, coaching, automatic creation)
2. Check the memory write gate for bypassed validation
3. Review misconception detection and acknowledgment flow
4. Check for any derivation that created false memories
5. Identify what data triggered the poisoning

### Recover
1. Acknowledge/retract poisoned memories through the coach menu
2. Reset mastery state to correct position
3. Re-run the affected learning sessions
4. Verify the mastery state machine is in correct state

### Verify
1. Test the mastery state machine transitions
2. Verify misconception detection and retraction
3. Run the learning evaluation mastery tests
4. Confirm memory isolation between users

### Post-Incident Review
1. Document what triggered the memory poisoning
2. Improve the memory write gate validation
3. Add additional checks for derived memory creation
4. Review the conflict resolution logic (§21)
5. Schedule memory integrity audits

---

## 7. Malicious File Upload

### Detection
- Antivirus/antimalware alerts on uploaded files
- Unusual file sizes or types
- Failed file processing errors
- User reports of unexpected behavior after upload

### Containment
1. Immediately remove the malicious file from storage
2. Quarantine the affected knowledge source
3. Disable file uploads temporarily if needed
4. Isolate the affected knowledge base

### Revoke/Rotate
1. No credentials to rotate
2. Re-index knowledge sources without the malicious file
3. Update file type/size validation rules if gaps found

### Investigate
1. Analyze the uploaded file type and content
2. Check the document processing pipeline (PDF, DOCX, EPUB, TXT, MD)
3. Review the chunk extraction and checksum validation
4. Identify what malicious content was in the file and how it was processed
5. Check for any code execution vectors that were nearly exploited

### Recover
1. Remove the malicious file and re-process affected knowledge sources
2. Re-enable file uploads with updated validation
3. Re-process knowledge sources with checksum verification
4. Test with known-safe files

### Verify
1. Test file upload with valid formats (PDF, DOCX, EPUB, TXT, MD)
2. Test size limits (50 MB max, 1000 PDF pages max)
3. Test path traversal protection (_resolve function)
4. Test checksum verification on re-read
5. Test duplicate prevention (UNIQUE owner_user_id + checksum)

### Post-Incident Review
1. Document the malicious file characteristics
2. Improve file type validation if gaps found
3. Update chunk extraction hardening
4. Add malware detection if appropriate
5. Schedule regular file security audits

---

## 8. Database Compromise

### Detection
- Unusual database query patterns
- Failed authentication attempts
- Data integrity check failures
- Reports of modified or deleted user data

### Containment
1. Restrict database access immediately
2. Enable database read-only mode if possible
3. Review and revoke excessive database permissions
4. Backup the current database state for investigation
5. Isolate the database server from network exposure

### Revoke/Rotate
1. Rotate database user passwords
2. Rotate any application database connection strings
3. Rotate any API keys or tokens that database access could authorize
4. Rotate any encryption keys used for data at rest

### Investigate
1. Review database audit logs for the compromise period
2. Identify what data was read, modified, or deleted
3. Check for privilege escalation or SQL injection
4. Identify the attack vector (code injection, credential theft, etc.)
5. Preserve all database logs and query evidence

### Recover
1. Restore database from clean backup if needed
2. Apply all security patches and updates
3. Restore database access with least-privilege principles
4. Monitor for recurrence of suspicious activity

### Verify
1. Run database integrity checks (PRAGMA integrity_check)
2. Verify all user data is intact and isolated
3. Test cross-user isolation
4. Verify all data operations work correctly
5. Check all schema migrations are applied

### Post-Incident Review
1. Document the full scope of the compromise
2. Review database access controls and permissions
3. Apply defense-in-depth measures (connection pooling, parameterized queries)
4. Implement database activity monitoring
5. Schedule regular database security assessments

---

## 9. Log/Secret Leakage

### Detection
- Secret scanning tools finding credentials in logs
- Users reporting visible tokens or keys
- Monitoring alerts for sensitive data in logs
- Security scan detecting plaintext secrets

### Containment
1. Immediately redact or remove leaked content from logs
2. Rotate any leaked credentials (tokens, API keys, passwords)
3. Disable log output containing secrets temporarily
4. Identify the source of the leak

### Revoke/Rotate
1. Rotate all leaked credentials immediately
2. Rotate the bot token if it appeared in logs
3. Rotate the Gemini API key if it appeared in logs
4. Rotate any database passwords found in logs
5. Update any other exposed secrets

### Investigate
1. Trace the log output path to find where secrets entered
2. Check for `print` statements that included sensitive data
3. Review the redaction/formatting formatter
4. Check for any `print` of user-generated content without scrubbing
5. Identify what logging level/configuration caused the leak

### Recover
1. Fix the redaction/formatting logic to cover all paths
2. Rotate all rotated credentials
3. Re-enable logging with proper redaction
4. Run log redaction validation tests

### Verify
1. Run the secret scan across all log files
2. Test that passwords, SSNs, and API keys are never in logs
3. Test that user messages are redacted appropriately
4. Verify the RedactingFormatter works for all output paths
5. Test with intentional secret inclusion to verify redaction

### Post-Incident Review
1. Document what secrets were leaked and how
2. Improve the redaction logic to cover all code paths
3. Add automated secret scanning to the CI/CD pipeline
4. Review all `print` statements and logging calls
5. Schedule regular log security audits

---

## Implementation Notes

### Automated Tests for Security Assumptions

The following tests should be part of the regression suite:

1. **Prompt Injection Test**: Verify untrusted content cannot override system policy
   - Test cases: user message, uploaded document, metadata, memory, context
   - Expected: injection blocked in all cases

2. **Cross-User Isolation Test**: Verify User A cannot access User B's data
   - Test cases: goals, habits, memories, knowledge sources
   - Expected: access denied in all cases

3. **Sensitive Data Protection Test**: Verify passwords/SSNs/bank accounts are not stored/logged
   - Test cases: sensitive topics in memory write gate
   - Expected: redaction active, passwords not stored, SSN not persisted

4. **File Type Validation Test**: Verify only accepted formats are processed
   - Test cases: PDF, DOCX, EPUB, TXT, MD; rejected types
   - Expected: accepted formats process correctly, rejected formats rejected

5. **Memory Write Gate Test**: Verify sensitive topics are rejected/parked
   - Test cases: passwords, SSNs, bank accounts, health data
   - Expected: rejected or consent_pending in all cases

6. **RAG Groundedness Test**: Verify answers use evidence markers only
   - Test cases: queryable from corpus, off-topic not fabricated, citations carry source identity, injection treated as data
   - Expected: all grounding properties verified

7. **RATE LIMIT / BACKPRESSURE TEST**: Verify rate limits prevent abuse
   - Test cases: per-user, per-operation, per-capability limits
   - Expected: rate limits enforced, safe failure messages

8. **Configuration Validation Test**: Verify production-safe defaults
   - Test cases: environment, log_level, fallback behavior
   - Expected: production requires WARNING+/ERROR log levels, development mode informative

These tests should be run as part of the regression harness (Item 21) and block CI/CD passes if any fail.

---

## Escalation Priorities

| Severity | Time to Respond | Escalation Path |
|---|---|---|
| CRITICAL | < 15 minutes | Team lead → Security lead → CTO |
| HIGH | < 1 hour | Team lead → Security lead |
| MEDIUM | < 4 hours | Team lead |
| LOW | < 24 hours | Team lead |

---

## Documentation References

- SECURITY_ARCHITECTURE.md - Threat model and preventive controls
- TELEGRAM_SECURITY.md - Telegram-specific security controls
- PRIVACY_RETENTION.md - Data retention and deletion rules
- TELEGRAM_E2E_PROCEDURE.md - E2E procedure with live credential requirements
- KNOWLEDGE_ARCHITECTURE.md - Knowledge processing security
- MEMORY_ARCHITECTURE.md - Memory security and isolation
- LEARNING_ARCHITECTURE.md - Learning engine security
- COACHING_ARCHITECTURE.md - Coach integration security
- SECURITY_INCIDENT_RESPONSE.md (this document)