# AUSTRO AI

A personal AI accountability and productivity Telegram bot. It combines goal tracking, daily planning, study/English/programming modes, habit building, nightly self-review ("المحاسبة"), coaching, and reminders into one Telegram assistant.

## Features

- 🎯 Goal tracking with progress updates
- 📅 Daily plan builder and checklists
- ⚡ Discipline mode with streaks
- 📚 Study, 🇬🇧 English, 💻 Programming modes (Gemini-powered)
- 🔄 Habit building with frequency tracking
- 🌙 Nightly review (المحاسبة) conversation
- 🤖 AI coaching via Google Gemini Flash (free tier) with a local fallback
- 🔔 Custom reminders restored from the database (daily or one-shot), plus fixed morning/noon/evening/night reminders
- 📊 Life dashboard with statistics
- 📖 Knowledge engine with book ingestion and RAG
- 🧠 Personal memory engine with typed facts
- 📈 Adaptive learning engine with mastery tracking
- 🛡️ Security: prompt injection defenses, data isolation, redaction

## Prerequisites

- Python 3.10+ (tested with 3.14 on Windows)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) A Google Gemini API key — without one the bot uses built-in local fallbacks

## Quick start

### Windows

```bat
setup.bat
```

### Linux / macOS

```bash
chmod +x setup.sh
./setup.sh
```

### Manual

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# edit .env and set BOT_TOKEN
python main.py
```

## Configuration

Create a `.env` file in the project root (copy from `.env.example`):

```
BOT_TOKEN=123456:your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key_optional
```

Full configuration options are documented in `.env.example` including:
- Environment (development | testing | production)
- Logging level (DEBUG | INFO | WARNING | ERROR)
- AI quotas, timeouts, and retry behavior
- Knowledge engine parameters (chunking, embeddings, retrieval)
- Memory engine parameters
- Learning engine parameters

`DB_PATH` (SQLite file) and `LOGS_PATH` are optional and default to `database/` and `logs/`.

## Chat commands (in Telegram)

`/start` · `/help` · `/plan` · `/goals` · `/habits` · `/progress` · `/review` · `/coach` · `/dashboard` · `/settings` · `/cancel`

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run offline: they set a dummy `BOT_TOKEN`, use a temporary SQLite database, and leave `GEMINI_API_KEY` unset so nothing hits the network.

**153 tests** (Phase A/B contract + Phase C knowledge + 57 Phase D memory + 27 Phase E learning + 13 Phase E coaching + 12 Phase F evaluation/performance/production).

### Evaluation & Regression (Phase F)

```bash
# Run all evaluation suites with thresholds
python scripts/verify.py

# JSON output only
python scripts/verify.py --json

# Show baseline comparison
python scripts/verify.py --baseline
```

Evaluation domains: RAG, Learning, Memory, Security (prompt injection), Coaching.

### Dependency Security Audit

```bash
python scripts/dependency_audit.py
```

### Performance Tests

```bash
python -m tests.test_performance
```

### CI/CD

GitHub Actions workflow in `.github/workflows/ci.yml` runs:
- Dependency installation
- Byte-compilation
- Pyflakes linting
- Pytest test suite
- Evaluation regression harness
- Dependency security audit (pip-audit)
- Performance tests
- Secret scanning (trufflehog)

## Project structure

```
main.py                Application entry point + wiring
handlers.py            All command/callback/conversation handlers
config.py              Env loading + prompts + responses
database.py            Thread-safe SQLite layer
ai_engine.py           Gemini client + local fallback
reminder_scheduler.py  Job-queue based reminders
redaction.py           Secret redaction for log output
debug_getme.py         Token/no-network diagnostic helpers
app/                   Core application modules
  ai/                  AI gateway, telemetry, providers
  coaching/            Coaching service
  config/              Settings, prompts
  core/                Container, errors, interfaces
  database/            Connection, migrations, repositories
  domain/              Entities, context
  evaluation/          Evaluation framework & runners
  infrastructure/      Scheduler
  knowledge/           Ingestion, retrieval, RAG
  learning/            Mastery, scheduling, assessment, curriculum
  memory/              Typed memory, write gate, retrieval
  observability/       Logging config
  security/            Redaction
  telegram/            Bot handlers, main
tests/                 pytest suite + evaluation + performance
scripts/               verify.py, dependency_audit.py
evals/datasets/        Golden datasets for evaluation
.github/workflows/     CI/CD pipeline
```

## Security notes

- Log output passes through a `RedactingFormatter` that masks bot tokens, API keys, and secrets.
- Do NOT commit `logs/bot.log`, `database/*.db`, `backups/`, or any `.env`.
- If your token ever leaked in logs (it did in historical `logs/bot.log`), revoke it via @BotFather → Revoke and generate a new token.
- Prompt injection defenses: user messages, document content, and metadata are treated as data, never as instructions.
- Owner-scoped isolation: every database row is bound to `owner_user_id` and filtered on read.
- Knowledge documents cannot feed personal memory (write gate enforcement).

## Documentation

- ARCHITECTURE.md — System architecture overview
- KNOWLEDGE_ARCHITECTURE.md — Knowledge engine (ingestion, RAG, embeddings)
- MEMORY_ARCHITECTURE.md — Personal memory engine (typed facts, write gate)
- LEARNING_ARCHITECTURE.md — Adaptive learning (mastery, scheduling, curriculum)
- COACHING_ARCHITECTURE.md — Coaching service
- SECURITY_ARCHITECTURE.md — Security model (isolation, redaction, injection defense)
- TELEGRAM_SECURITY.md — Telegram-specific security considerations
- PRIVACY_RETENTION.md — Data privacy and retention policies
- SECURITY_INCIDENT_RESPONSE.md — Security incident response procedures
- TELEGRAM_E2E_PROCEDURE.md — End-to-end Telegram testing procedure
- PHASE_F_FINAL_REPORT.md — Phase F completion report

## Phase F Status

Phase F (Evaluation, CI/CD, Security, Performance, Production Config) — **COMPLETE**

- ✅ Item 21: Regression harness with deterministic fixtures (`scripts/verify.py`)
- ✅ Item 22: CI/CD quality gates (compile, lint, test, eval, security, perf) — `.github/workflows/ci.yml`
- ✅ Item 23: Dependency security audit (pip-audit, zero vulns) — `scripts/dependency_audit.py`
- ✅ Item 24: Performance tests (retrieval, memory, learning, gateway, concurrent) — `tests/test_performance.py`
- ✅ Item 25: Production configuration (validated, documented) — `app/config/settings.py`
- ✅ Item 26: Telegram E2E procedure (documented, blocked without credentials) — `TELEGRAM_E2E_PROCEDURE.md`
- ✅ Item 27: Security incident response (documented) — `SECURITY_INCIDENT_RESPONSE.md`
- ✅ Item 28: Documentation synchronization — all 12 architecture docs verified against implementation

## License

Private / internal use.