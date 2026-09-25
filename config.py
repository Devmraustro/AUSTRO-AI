"""
AUSTRO AI - Configuration shim.

Phase B: settings and prompt constants now live in the validated `app.config`
package. This module re-exports the legacy names so existing imports such as
`from config import BOT_TOKEN, ARABIC_RESPONSES, LOGS_PATH` keep working
unchanged.
"""

from pathlib import Path

from app.config.prompts import (
    ARABIC_RESPONSES,
    GEMINI_COACH_PROMPT,
    GEMINI_CYBER_PROMPT,
    GEMINI_ENGLISH_PROMPT,
    GEMINI_PROGRAMMING_PROMPT,
    GEMINI_STUDY_PROMPT,
    GEMINI_SYSTEM_PROMPT,
    LOCAL_AI_RESPONSES,
    MODES,
    REMINDER_TIMES,
    SYSTEMS,
)
from app.config.settings import BASE_DIR, settings

BOT_TOKEN = settings.bot_token
BOT_NAME = settings.bot_name
BOT_USERNAME = settings.bot_username
DB_PATH = settings.db_path
GEMINI_API_KEY = settings.gemini_api_key
GEMINI_MODEL = settings.gemini_model
GEMINI_API_URL = settings.gemini_api_url
USE_LOCAL_FALLBACK = settings.use_local_fallback

WEBHOOK_URL = settings.webhook_url
WEBHOOK_PORT = settings.webhook_port
WEBHOOK_SECRET = settings.webhook_secret

DB_ENGINE = settings.db_engine

DB_DIR = Path(settings.db_path).parent
LOGS_DIR = Path(settings.logs_path)
BACKUPS_DIR = Path(settings.backups_path)
LOGS_PATH = settings.logs_path
BACKUP_PATH = settings.backups_path

__all__ = [
    "BASE_DIR",
    "BOT_TOKEN",
    "BOT_NAME",
    "BOT_USERNAME",
    "DB_DIR",
    "DB_PATH",
    "BACKUPS_DIR",
    "BACKUP_PATH",
    "LOGS_DIR",
    "LOGS_PATH",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_API_URL",
    "USE_LOCAL_FALLBACK",
    "WEBHOOK_URL",
    "WEBHOOK_PORT",
    "WEBHOOK_SECRET",
    "DB_ENGINE",
    "LOCAL_AI_RESPONSES",
    "MODES",
    "SYSTEMS",
    "ARABIC_RESPONSES",
    "GEMINI_SYSTEM_PROMPT",
    "GEMINI_STUDY_PROMPT",
    "GEMINI_ENGLISH_PROMPT",
    "GEMINI_PROGRAMMING_PROMPT",
    "GEMINI_CYBER_PROMPT",
    "GEMINI_COACH_PROMPT",
    "REMINDER_TIMES",
]