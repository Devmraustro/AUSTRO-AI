"""
AUSTRO AI - Centralized validated configuration.

Every runtime value lives here and is validated once at startup. Modules
depend on `settings` instead of reading environment variables directly, which
removes hardcoded keys, URLs, models, quotas and timeouts from the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

from app.core.errors import ConfigurationError

BASE_DIR = Path(__file__).resolve().parent.parent.parent

_DEFAULT_SYSTEM_PROMPTS: Dict[str, str] = {}


@dataclass(frozen=True)
class Settings:
    """Validated application settings."""

    # Required fields (no defaults)
    bot_token: str
    bot_name: str
    bot_username: str

    db_path: str
    logs_path: str
    backups_path: str

    gemini_api_key: str
    gemini_model: str
    gemini_api_url: str
    use_local_fallback: bool

    # Knowledge engine (book ingestion + RAG)
    knowledge_storage_path: str
    knowledge_max_file_size_mb: int
    knowledge_max_pages: int
    knowledge_max_chars: int
    knowledge_max_chunks: int
    knowledge_chunk_size: int
    knowledge_chunk_overlap: int
    knowledge_embedding_dimensions: int
    knowledge_embedding_model: str
    knowledge_embedding_version: str
    knowledge_retrieval_top_k: int
    knowledge_retrieval_min_score: float
    knowledge_context_budget_chars: int
    knowledge_embedding_batch_size: int

    # Personal memory engine (typed memory + context packs)
    memory_max_retrieved: int
    memory_context_budget_chars: int
    memory_max_candidates: int

    # Adaptive learning engine (curriculum, sessions, schedules, planning)
    learning_use_llm: bool
    learning_max_lessons_cache: int
    learning_default_session_minutes: int
    learning_review_minutes: int
    learning_planner_max_items: int

    # AI tuning (removes hardcoded quotas/timeouts/backoff from call sites)
    max_requests_per_day: int
    min_request_interval: float
    ai_request_timeout: float
    ai_healthcheck_timeout: float
    ai_max_retries: int
    ai_retry_backoff_base: float

    # Optional fields with defaults
    environment: str = "development"
    log_level: str = "INFO"
    webhook_url: str = ""
    webhook_port: int = 8443
    webhook_secret: str = ""

    # Database settings
    db_engine: str = "sqlite"  # "sqlite" | "postgresql"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "austro_ai"
    db_user: str = "austro"
    db_password: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # Application-level rate limits (per-user, deterministic; see rate_limiter)
    rate_limit_command_max: int = 30
    rate_limit_command_window: float = 60.0
    rate_limit_ai_max: int = 20
    rate_limit_ai_window: float = 60.0
    rate_limit_upload_max: int = 10
    rate_limit_upload_window: float = 3600.0
    rate_limit_ingestion_max: int = 5
    rate_limit_ingestion_window: float = 3600.0
    rate_limit_expensive_max: int = 10
    rate_limit_expensive_window: float = 3600.0
    rate_limit_global_ai_max: int = 100
    rate_limit_global_ai_window: float = 60.0

    gemini_model: str = "gemini-2.5-flash"
    gemini_api_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    use_local_fallback: bool = True

    # Knowledge engine (book ingestion + RAG)
    knowledge_storage_path: str = ""
    knowledge_max_file_size_mb: int = 50
    knowledge_max_pages: int = 1000
    knowledge_max_chars: int = 2000000
    knowledge_max_chunks: int = 20000
    knowledge_chunk_size: int = 900
    knowledge_chunk_overlap: int = 100
    knowledge_embedding_dimensions: int = 128
    knowledge_embedding_model: str = "local-hash"
    knowledge_embedding_version: str = "1"
    knowledge_retrieval_top_k: int = 8
    knowledge_retrieval_min_score: float = 0.20
    knowledge_context_budget_chars: int = 6000
    knowledge_embedding_batch_size: int = 16

    # Personal memory engine (typed memory + context packs)
    memory_max_retrieved: int = 12
    memory_context_budget_chars: int = 1800
    memory_max_candidates: int = 200

    # Adaptive learning engine (curriculum, sessions, schedules, planning)
    learning_use_llm: bool = False
    learning_max_lessons_cache: int = 200
    learning_default_session_minutes: int = 25
    learning_review_minutes: int = 5
    learning_planner_max_items: int = 8

    # AI tuning (removes hardcoded quotas/timeouts/backoff from call sites)
    max_requests_per_day: int = 1500
    min_request_interval: float = 0.5
    ai_request_timeout: float = 30.0
    ai_healthcheck_timeout: float = 15.0
    ai_max_retries: int = 2
    ai_retry_backoff_base: float = 0.5

    system_prompts: Dict[str, str] = field(default_factory=dict)

    # Production-specific settings
    webhook_url: str = ""
    webhook_port: int = 8443
    webhook_secret: str = ""
    log_level: str = "INFO"

    # Database settings
    db_engine: str = "sqlite"  # "sqlite" | "postgresql"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "austro_ai"
    db_user: str = "austro"
    db_password: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key) and len(self.gemini_api_key) >= 10


def _resolve_absolute(base: Path, value: str) -> str:
    if os.path.isabs(value):
        return value
    return str(base / value)


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()


def load_settings() -> Settings:
    """Load and validate configuration from the environment / .env file."""
    _load_dotenv()

    db_dir = BASE_DIR / "database"
    logs_dir = BASE_DIR / "logs"
    backups_dir = BASE_DIR / "backups"
    knowledge_dir = BASE_DIR / "knowledge_storage"
    db_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token or bot_token == "your_bot_token_here":
        raise ConfigurationError(
            "❌ BOT_TOKEN not set or is placeholder!\n"
            "Please create a .env file in the project folder with:\n"
            "BOT_TOKEN=your_real_token_here\n"
            f"Looking in: {BASE_DIR}"
        )

    db_path = _resolve_absolute(BASE_DIR, os.getenv("DB_PATH", str(db_dir / "austro_ai.db")))

    settings = Settings(
        bot_token=bot_token,
        bot_name="AUSTRO AI",
        bot_username="austro_ai_bot",
        db_path=db_path,
        logs_path=str(logs_dir),
        backups_path=str(backups_dir),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_api_url=os.getenv(
            "GEMINI_API_URL",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        use_local_fallback=os.getenv("USE_LOCAL_FALLBACK", "true").lower() in ("1", "true", "yes"),
        knowledge_storage_path=str(knowledge_dir),
        knowledge_max_file_size_mb=int(os.getenv("KNOWLEDGE_MAX_FILE_SIZE_MB", "50")),
        knowledge_max_pages=int(os.getenv("KNOWLEDGE_MAX_PAGES", "1000")),
        knowledge_max_chars=int(os.getenv("KNOWLEDGE_MAX_CHARS", "2000000")),
        knowledge_max_chunks=int(os.getenv("KNOWLEDGE_MAX_CHUNKS", "20000")),
        knowledge_chunk_size=int(os.getenv("KNOWLEDGE_CHUNK_SIZE", "900")),
        knowledge_chunk_overlap=int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "100")),
        knowledge_embedding_dimensions=int(
            os.getenv("KNOWLEDGE_EMBEDDING_DIMENSIONS", "128")
        ),
        knowledge_embedding_model=os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "local-hash"),
        knowledge_embedding_version=os.getenv("KNOWLEDGE_EMBEDDING_VERSION", "1"),
        knowledge_retrieval_top_k=int(os.getenv("KNOWLEDGE_RETRIEVAL_TOP_K", "8")),
        knowledge_retrieval_min_score=float(
            os.getenv("KNOWLEDGE_RETRIEVAL_MIN_SCORE", "0.20")
        ),
        knowledge_context_budget_chars=int(
            os.getenv("KNOWLEDGE_CONTEXT_BUDGET_CHARS", "6000")
        ),
        knowledge_embedding_batch_size=int(
            os.getenv("KNOWLEDGE_EMBEDDING_BATCH_SIZE", "16")
        ),
        memory_max_retrieved=int(os.getenv("MEMORY_MAX_RETRIEVED", "12")),
        memory_context_budget_chars=int(
            os.getenv("MEMORY_CONTEXT_BUDGET_CHARS", "1800")
        ),
        memory_max_candidates=int(os.getenv("MEMORY_MAX_CANDIDATES", "200")),
        learning_use_llm=os.getenv("LEARNING_USE_LLM", "false").lower() in ("1", "true", "yes"),
        learning_max_lessons_cache=int(os.getenv("LEARNING_MAX_LESSONS_CACHE", "200")),
        learning_default_session_minutes=int(os.getenv("LEARNING_DEFAULT_SESSION_MINUTES", "25")),
        learning_review_minutes=int(os.getenv("LEARNING_REVIEW_MINUTES", "5")),
        learning_planner_max_items=int(os.getenv("LEARNING_PLANNER_MAX_ITEMS", "8")),
        max_requests_per_day=int(os.getenv("AI_MAX_REQUESTS_PER_DAY", "1500")),
        min_request_interval=float(os.getenv("AI_MIN_REQUEST_INTERVAL", "0.5")),
        ai_request_timeout=float(os.getenv("AI_REQUEST_TIMEOUT", "30")),
        ai_healthcheck_timeout=float(os.getenv("AI_HEALTHCHECK_TIMEOUT", "15")),
        ai_max_retries=int(os.getenv("AI_MAX_RETRIES", "2")),
        ai_retry_backoff_base=float(os.getenv("AI_RETRY_BACKOFF_BASE", "0.5")),
        environment=os.getenv("AUSTRO_ENVIRONMENT", "development"),
        log_level=os.getenv("AUSTRO_LOG_LEVEL", "INFO"),
        webhook_url=os.getenv("WEBHOOK_URL", ""),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8443")),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        db_engine=os.getenv("DB_ENGINE", "sqlite"),
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "austro_ai"),
        db_user=os.getenv("DB_USER", "austro"),
        db_password=os.getenv("DB_PASSWORD", ""),
        db_pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        db_max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        db_pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
        db_pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        rate_limit_command_max=int(os.getenv("RATE_LIMIT_COMMAND_MAX", "30")),
        rate_limit_command_window=float(os.getenv("RATE_LIMIT_COMMAND_WINDOW", "60")),
        rate_limit_ai_max=int(os.getenv("RATE_LIMIT_AI_MAX", "20")),
        rate_limit_ai_window=float(os.getenv("RATE_LIMIT_AI_WINDOW", "60")),
        rate_limit_upload_max=int(os.getenv("RATE_LIMIT_UPLOAD_MAX", "10")),
        rate_limit_upload_window=float(os.getenv("RATE_LIMIT_UPLOAD_WINDOW", "3600")),
        rate_limit_ingestion_max=int(os.getenv("RATE_LIMIT_INGESTION_MAX", "5")),
        rate_limit_ingestion_window=float(os.getenv("RATE_LIMIT_INGESTION_WINDOW", "3600")),
        rate_limit_expensive_max=int(os.getenv("RATE_LIMIT_EXPENSIVE_MAX", "10")),
        rate_limit_expensive_window=float(os.getenv("RATE_LIMIT_EXPENSIVE_WINDOW", "3600")),
        rate_limit_global_ai_max=int(os.getenv("RATE_LIMIT_GLOBAL_AI_MAX", "100")),
        rate_limit_global_ai_window=float(os.getenv("RATE_LIMIT_GLOBAL_AI_WINDOW", "60")),
    )

    # Production environment validation
    prod_violations = []
    if settings.environment == "production":
        if settings.log_level not in ("WARNING", "ERROR"):
            prod_violations.append(
                f"production environment requires log_level WARNING or ERROR, "
                f"got '{settings.log_level}'"
            )
        if not settings.webhook_url:
            prod_violations.append(
                "production environment requires WEBHOOK_URL to be set"
            )
        if not settings.webhook_secret:
            prod_violations.append(
                "production environment requires WEBHOOK_SECRET to be set"
            )
        if settings.use_local_fallback:
            prod_violations.append(
                "production environment should not use local fallback (USE_LOCAL_FALLBACK=false)"
            )
        if settings.gemini_api_key == "":
            prod_violations.append(
                "production environment requires GEMINI_API_KEY to be set"
            )
    if prod_violations:
        raise ConfigurationError(
            "❌ " + " ".join(prod_violations)
        )

    # Development/local information
    if settings.environment == "development":
        print(f"Development environment: log_level={settings.log_level}, "
              f"use_local_fallback={settings.use_local_fallback}")

    # Validate log level is recognized
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    if settings.log_level not in valid_levels:
        raise ConfigurationError(
            f"❌ Invalid log_level '{settings.log_level}'. "
            f"Must be one of: {', '.join(valid_levels)}"
        )

    return settings


settings = load_settings()


__all__: List[str] = [
    "Settings",
    "load_settings",
    "settings",
    "BASE_DIR",
    "ConfigurationError",
]