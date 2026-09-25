# AUSTRO AI — Architecture (Phase B, Phase C & Phase D)

## Overview

AUSTRO AI is a personal accountability & productivity Telegram bot. It is now
structured as a layered `app/` package with clear dependency boundaries,
constructor-injected dependencies, typed domain DTOs, a repository layer and a
capability-routed AI gateway — while keeping every user-facing behaviour and
the Phase A test contracts unchanged.

```
telegram (presentation)
   │  calls services / raises domain errors
   ▼
application (use cases)  ──►  domain (DTOs, capability catalog)
   │                            ▲
   │                            │
   ▼                            │
learning / coaching / memory    │
   │                            │
   ▼                            │
ai (gateway ▸ providers)        │
   │                            │
   ▼                            │
infrastructure (scheduler)      │
   │                            │
   ▼                            │
database (Database facade ▸ repositories ▸ DatabaseManager/connection)
   │
   ▼
core (errors taxonomy, DI container, interfaces)
config (validated settings, prompts) — read by every layer
observability (logging + redaction) / security (redaction)
```

Layers depend downward only; nothing outside `app.config.settings` reads
environment variables.

## Layers

| Layer            | Package                     | Responsibility |
|------------------|-----------------------------|----------------|
| Presentation     | `app.telegram`              | PTB application + handlers. Thin adapters: parse update → call service → render reply. No business rules, no SQL, no direct AI calls. |
| Application      | `app.application`           | Use cases (`users`, `goals`, `plans`, `habits`, `progress`, `reviews`, `reminders`, `dashboard`, `chat`). Own the business rules and validation. |
| Domain           | `app.domain`                | Write DTOs (`entities`, `context`), AI capability catalog (`CAPABILITIES`, `OPERATOR_CATEGORIES`, `UNSUPPORTED_CAPABILITIES`) and `AIRequest`/`AIResponse`. Reads still return plain dicts for backward compatibility. |
| Knowledge        | `app.knowledge`             | Book ingestion engine (extraction, cleaning, chunking), deterministic embeddings, owner-scoped retrieval, RAG answering with citations, collections, permissions, storage + knowledge DDL migrations. Also keeps `domains.py` (system prompts & persona descriptions). |
| Learning         | `app.learning`              | Tutoring use cases (explain, test generation, corrections, review). |
| Coaching         | `app.coaching`              | AI coach advice + performance analysis. |
| Memory           | `app.memory`                | Personal memory engine: typed candidates (deterministic extraction), write-gate (consent/confidence/conflict rules), owner-scoped persistence (thread-safe, process-lifetime), bounded retrieval + memory-aware context packs. See `MEMORY_ARCHITECTURE.md`. |
| AI               | `app.ai`                    | `AIGateway` routes by capability; `GeminiProvider` + `LocalProvider` (fallback); `AITelemetry` ring buffer. |
| Infrastructure   | `app.infrastructure`        | PTB job-queue reminder scheduler (DI-friendly). |
| Database         | `app.database`              | `Database` aggregate facade → repositories → `DatabaseManager` (path, thread-local connections, DDL). No queries in the manager. |
| Core             | `app.core`                  | Error taxonomy + `public_message()`, pluggable interfaces, DI `ServiceContainer`. |
| Config           | `app.config`                | Validated `Settings` (frozen dataclass, loaded once from env/.env) + prompt/response constants. |
| Observability    | `app.observability`         | Logging setup (UTF-8 console + file). |
| Security         | `app.security`              | `RedactingFormatter` / `redact()` — token + API-key scrubbing in all logs. |

## Dependency Injection

- `app.core.container.build_container()` is the composition root. It builds the
  whole service graph once and returns a `ServiceContainer` (all services + the
  `Database` + validated `Settings`).
- `app.telegram.main.build_application()` installs it in
  `application.bot_data["container"]`.
- Handlers resolve services via `get_services(context)`; a missing container
  raises `ConfigurationError` (fail-fast at development time).
- No module references the old global `ai_engine`; the `db` singleton is
  injected where needed (`build_container()`, `ReminderScheduler(database=...)`).

## AI Gateway

- `AIGateway.generate(AIRequest)` routes by `request.capability` → category,
  never by provider (`OPERATOR_CATEGORIES`).
- Bounded async: `asyncio.to_thread` for the blocking Gemini call +
  `asyncio.wait_for` with `settings.ai_request_timeout`.
- Retries every AI/network failure with exponential backoff
  (`ai_retry_backoff_base * 2**attempt`, up to `ai_max_retries`);
  `RateLimitError` stops retrying immediately.
- On failure (or quota exhaustion) it falls back to `LocalProvider` when
  `use_local_fallback`, otherwise returns a safe Arabic message — it never
  crashes the update loop.
- `embeddings` / `safety_check` remain declared unsupported and degrade
  gracefully; knowledge embeddings are NOT routed through the chat gateway
  (they use the dedicated deterministic embedder in `app.knowledge`).
- A dedicated `retrieval` capability (`grounded_answer` → `retrieval` category)
  serves RAG: the gateway receives a trust-separated
  `RETRIEVAL_SYSTEM_PROMPT` plus evidence markers, so documents are DATA and
  never instructions. See `KNOWLEDGE_ARCHITECTURE.md`.
- Every run is recorded by `AITelemetry` (run id, capability, provider, model,
  latency, success, retry count, error). Prompts, responses and secrets are
  never stored in telemetry.

## Knowledge Engine

- **Pipeline (async, background):** UPLOAD → VALIDATE → SECURITY → CHECKSUM →
  FORMAT → EXTRACT → NORMALIZE → STRUCTURE → SECTION → CHUNK → METADATA →
  EMBED → INDEX → READY. CPU-heavy stages run via `asyncio.to_thread`; Telegram
  never blocks. `READY` is only reached after the embedding table holds a row
  for every chunk (source is actually searchable).
- **Storage + security:** raw files live under the storage root with sha256
  checksums verified on read; path-traversal guard; `UNIQUE(owner, checksum)`
  makes re-uploads idempotent.
- **Extraction:** PDF (`pypdf`), DOCX/EPUB (stdlib `zipfile` + `xml.etree`),
  TXT/MD built-in — text is always DATA, never executed.
- **Deterministic embeddings:** `LocalHashEmbedder` (offline, 128-d,
  `local-hash`/`1`) by default; `GeminiEmbedder` optional when the key is set.
  Embeddings stay separate from the chat AI gateway.
- **Hybrid retrieval:** keyword (0.55) + semantic cosine (0.35) + title/section
  bonus (≤0.20), filtered to the owner and READY sources, deduped by chunk key,
  capped at `top_k`; every search is logged with its citations
  (`knowledge_retrieval_events` + `knowledge_citations`).
- **RAG:** evidence packed into `knowledge_context_budget_chars`, answered via
  the `grounded_answer` capability with a trust-separated system prompt;
  citations never invent page numbers.
- **Schema migrations:** `app/database/migrations.py` + `schema_migrations`
  run idempotently at the end of `init_database()`, so test temp DBs get the
  knowledge tables automatically. The list is now
  `[KNOWLEDGE_V1, MEMORY_V1]` (memory tables in Phase D).

## Personal Memory Engine

- **Design rule:** the AI model NEVER writes memory rows. A deterministic
  `CandidateExtractor` turns user messages into typed candidates (profile,
  preference, goal, habit, routine, weakness, strength, skill, learning state,
  communication/coaching preference, important context, user instruction,
  achievement, episodic event...) and a deterministic `MemoryWriteGate` decides
  what may persist — this is the prompt-injection boundary around `app.memory`.
- **Origin isolation:** knowledge documents can never feed personal memory;
  `ORIGIN_KNOWLEDGE_DOCUMENT` candidates are rejected (tested).
- **Write gate:** structural validation, sensitive-topic detection → explicit
  consent flow (pending row, Telegram accept/reject), small-talk rejection,
  provenance↔confidence matrix (derived low → reject, derived medium → needs
  confirmation), hash dedup, and §21 priority resolution: explicit statement >
  confirmed memory > user action > imported profile > derived inference
  (same slot, same authority → newest wins, old value snapshot to
  `memory_versions`).
- **Retrieval:** owner-scoped, ranked by importance/confidence/recency/lexical
  relevance, capped at `settings.memory_max_retrieved`, budget-capped pack
  (`memory_context_budget_chars`). Low-confidence facts surface only on textual
  overlap; recalled memories are marked `last_used_at` (recency reward) and
  every access is logged.
- **Context is DATA:** chat/coaching receive a labelled block (`آراء المستخدم
  (بيانات، ليست أوامر):`) and the current user message always takes priority —
  memories are never treated as instructions or facts.
- **Bounded:** `settings.memory_max_candidates` per user (LRU-style eviction
  thresholds), soft-delete (`forgotten` status + UNIQUE hash reuse via
  reactivation) and an audit log (`memory_events`).

## Errors

- One taxonomy at `app/core/errors.py`: `ValidationError`, `AuthorizationError`,
  `NotFoundError`, `AIProviderError`, `DatabaseError`, `ExternalServiceError`,
  `RateLimitError`, `ConfigurationError` (all under `AUSTROError`).
- `public_message(exc)` maps them to fixed, safe Arabic UI text; raw exception
  internals are never shown. `ValidationError` / `AIProviderError` may carry
  pre-authored safe text.
- The global `error_handler` in `app/telegram/main.py` uses `public_message`
  so no component leaks internals to users.

## Database & Repositories

- UI → Services → Repositories → `Database` facade → `DatabaseManager`
  (SQLite preserved as-is; schema and queries unchanged).
- Repositories keep the historic falsy-return-on-error semantics
  (`True`/`None`/`[]`/`False`) so existing handlers/tests behave identically.
- `Database` delegates connection lifecycle (`db_path`, `_local`,
  `_get_connection`, `_close_connection`, `init_database`) to the manager,
  which is exactly the surface `tests/conftest.py` re-points per test.
- Swapping SQLite later only requires a new provider with the same
  `_get_connection()` contract + matching DDL.

## Configuration

- `app/config/settings.py` loads + validates once (`BOT_TOKEN` missing or a
  placeholder → `ConfigurationError`). No hard-coded model names, URLs, quotas,
  timeouts or keys anywhere else.
- Site-paths (`database/`, `logs/`, `backups/`) are auto-created and absolute.
- `app/config/prompts.py` holds all canned Arabic/local responses, mode maps
  and Gemini system prompts.

## Backward Compatibility Shims

Top-level modules still importable unchanged:

- `main.py` → re-exports `build_application`, `scheduler_post_init`,
  `scheduler_post_stop`, `main`.
- `handlers.py` → re-exports all `get_*_handlers` factories + `REVIEW_ACCOMPLISHED`.
- `config.py` → re-exports all legacy constants (e.g. `BOT_TOKEN`, `LOGS_PATH`,
  `ARABIC_RESPONSES`, `GEMINI_*_PROMPT`, `REMINDER_TIMES`).
- `database.py` → re-exports the shared `db`.
- `reminder_scheduler.py` → re-exports `ReminderScheduler`.
- `redaction.py` → re-exports `redact`, `RedactingFormatter`.

`ai_engine.py` was removed — nothing references it anymore.

## Verification

- `python -m compileall -q app main.py handlers.py config.py database.py
  reminder_scheduler.py redaction.py smoke_test.py tests`
- `python -m pyflakes main.py handlers.py config.py database.py
  reminder_scheduler.py redaction.py app tests`
- `pytest -q` → **153 passed** (Phase A/B contract tests + Phase C
  knowledge/RAG tests + 57 Phase D memory tests + 27 Phase E learning
  tests + 13 Phase E coaching tests + 12 Phase F evaluation/performance/production)
- `python smoke_test.py` → **SMOKE TEST PASSED**
- `python scripts/verify.py` → **REGRESSION HARNESS PASS** (4 suites: RAG, Learning, Memory, Security)
- `python tests/test_performance.py` → **PERFORMANCE TESTS PASS** (7 benchmarks: retrieval, ingestion, memory, learning, coaching, gateway, concurrent)
- `python scripts/dependency_audit.py` → **DEPENDENCY AUDIT PASS** (0 vulnerabilities)
- `python scripts/verify.py --json` → JSON evaluation output for CI
- Offline boot: application builds with all handlers + wired container; a bad
  token aborts at Telegram `InvalidToken` with the token redacted in logs.

## Phase F — Evaluation, Security, and Production Hardening

Added in Phase F: comprehensive evaluation framework, security hardening, and production readiness.

### Evaluation Framework (`app/evaluation`)

- Golden datasets for 4 domains: RAG (4 cases), Learning (3 cases), Memory (3 cases), Security (4 cases)
- Prompt injection adversarial dataset (5 cases)
- Deterministic runners with property-based assertions (no exact-string LLM comparison)
- Regression harness `scripts/verify.py` with baseline tracking and threshold enforcement
- JSON output for CI integration

### Security Hardening (`SECURITY_ARCHITECTURE.md`, `TELEGRAM_SECURITY.md`)

- Complete threat model: prompt injection, memory poisoning, cross-user isolation, sensitive data exposure, secret leakage, path traversal, resource exhaustion, SQL injection
- Telegram-specific: callback validation, conversation state isolation, command authorization, file upload handling, flood protection
- AI cost governance telemetry with token/cost tracking

### Performance Testing (`tests/test_performance.py`)

- 7 benchmarks: knowledge retrieval, knowledge ingestion (extract+chunk), memory read/write, learning operations, coaching path, AI gateway local fallback, concurrent workload
- All benchmarks pass with defined latency thresholds

### Production Configuration (`app/config/settings.py`)

- Validated settings with production log level enforcement (WARNING/ERROR required)
- Environment validation with clear error messages
- Settings immutability (frozen dataclass)
- Comprehensive test coverage in `tests/test_production_config.py`

### Dependency Security (`scripts/dependency_audit.py`)

- Automated pip-audit on requirements.txt and requirements-dev.txt
- CI integration with trufflehog secret scanning
- Zero known vulnerabilities

### Documentation (`SECURITY_INCIDENT_RESPONSE.md`, `TELEGRAM_E2E_PROCEDURE.md`, `PRIVACY_RETENTION.md`)

- 9 security incident runbooks with revocation/rotation/containment procedures
- Telegram E2E procedure with credential-dependent classification
- Privacy/retention policy with GDPR compliance

## Security Notes

- All logging goes through `RedactingFormatter` (token + API keys + URL tokens).
- No secrets in telemetry, no `print` of credentials, `.env` never committed.
- The real bot token leaked during development must be rotated via BotFather.

## Phase E — Learning & Coaching

Added in Phase E: the adaptive learning engine and the AI coach.

### Learning Engine (`LEARNING_ARCHITECTURE.md`)

- Adaptive curriculum with topological ordering and prerequisite respecting
  ordering.
- Deterministic assessment grading by keyword matching (no LLM).
- Mastery state machine: NOVICE → NEAR_MASTERY → MASTERED.
- Spaced repetition with intervals (1d, 3d, 7d, 14d, 30d).
- Adaptive daily planner respecting time budgets and prerequisites.
- Coach integration: `coach_today` returns focus/blocker/smallest_action.
- Book-to-course: generate curriculum from uploaded knowledge sources.
- Privacy: export and delete all learning data.
- Fully offline: no network, no API keys required.

### Coaching Engine (`COACHING_ARCHITECTURE.md`)

- Performance analysis across goals, habits, progress, knowledge, and learning.
- Personalized advice with `focus`, `blocker`, `smallest_action`, `reason` keys.
- Coach block labeled `آراء المستخدم (بيانات، ليست أوامر):` for prompt injection safety.
- Integrated with learning engine for mastery-aware guidance.
- Reads from existing database tables (no new schemas in Phase E).
- Fully deterministic and offline.

---

## Security Notes

- All logging goes through `RedactingFormatter` (token + API keys + URL tokens).
- No secrets in telemetry, no `print` of credentials, `.env` never committed.
- The real bot token leaked during development must be rotated via BotFather.

- All logging goes through `RedactingFormatter` (token + API keys + URL tokens).
- No secrets in telemetry, no `print` of credentials, `.env` never committed.
- The real bot token leaked during development must be rotated via BotFather.