"""
AUSTRO AI - Deployment safety validation.

One place that decides whether a configuration is safe to deploy in
`production` or in `staging`, separated from `load_settings()` so the rules can
be unit-tested without importing the process-wide singleton.

The two environments are treated deliberately differently:

| Check                          | production | staging | Why                                        |
|--------------------------------|-----------|---------|--------------------------------------------|
| local fallback disabled        | required   | required| parity: a real AI provider path is tested   |
| Gemini API key configured      | required   | required| parity: same failure modes                 |
| webhook URL + secret configured| required   | required| parity: secrets wired the same way          |
| PostgreSQL engine              | -          | required| staging always runs against a real PG       |
| database name                  | -          | required| must be a dedicated, staging-marked DB      |
| database credentials           | -          | required| staging must not rely on an implicit login  |
| log level                      | WARNING/ERR| any     | staging keeps INFO/DEBUG for diagnosis      |
| Telegram transport             | -          | required| only `polling` is implemented (see below)   |
| staging identifier             | -          | required| proves an intentional staging deployment    |

`WEBHOOK_URL`/`WEBHOOK_SECRET` are still only *configuration* for both
environments: the runtime currently long-polls (see `app/telegram/main.py`).
Rather than silently ignoring a webhook configuration, an unsupported
`TELEGRAM_TRANSPORT=webhook` is rejected outright.
"""

from __future__ import annotations

from typing import List

PRODUCTION = "production"
STAGING = "staging"
DEPLOYED_ENVIRONMENTS = (PRODUCTION, STAGING)

#: Substrings that identify a non-production database name.
STAGING_DB_MARKERS = ("staging", "stage", "preprod", "pre-prod", "uat", "sbx")
#: Database name used by production; staging must never point at it.
PRODUCTION_DB_NAME = "austro_ai"
#: Bot-token fragments that are test/CI placeholders, never deployable.
PLACEHOLDER_TOKEN_MARKERS = ("dummy", "smoke", "example", "test-token", "your_bot_token")
SUPPORTED_TRANSPORTS = ("polling",)


def _transport_violations(transport: str, environment: str) -> List[str]:
    if transport not in SUPPORTED_TRANSPORTS:
        return [
            f"{environment} environment does not support "
            f"TELEGRAM_TRANSPORT='{transport}' (supported: "
            f"{', '.join(SUPPORTED_TRANSPORTS)}); the runtime uses long polling"
        ]
    return []


def production_violations(settings) -> List[str]:
    """Rules for `AUSTRO_ENVIRONMENT=production` (unchanged behaviour)."""
    violations: List[str] = []
    if settings.log_level not in ("WARNING", "ERROR"):
        violations.append(
            f"production environment requires log_level WARNING or ERROR, "
            f"got '{settings.log_level}'"
        )
    if not settings.webhook_url:
        violations.append(
            "production environment requires WEBHOOK_URL to be set"
        )
    if not settings.webhook_secret:
        violations.append(
            "production environment requires WEBHOOK_SECRET to be set"
        )
    if settings.use_local_fallback:
        violations.append(
            "production environment should not use local fallback (USE_LOCAL_FALLBACK=false)"
        )
    if settings.gemini_api_key == "":
        violations.append(
            "production environment requires GEMINI_API_KEY to be set"
        )
    return violations


def staging_violations(settings) -> List[str]:
    """Extra deployment-safety rules for the isolated staging slot.

    These exist so a staging deployment cannot quietly become a production
    deployment: wrong database, missing credentials, prod-only AI parity gaps
    or an unimplemented transport all fail closed at startup.
    """
    violations: List[str] = []
    if settings.use_local_fallback:
        violations.append(
            "staging environment must disable local fallback for production "
            "parity (USE_LOCAL_FALLBACK=false)"
        )
    if settings.gemini_api_key == "":
        violations.append(
            "staging environment requires GEMINI_API_KEY to be set "
            "(production parity)"
        )
    if not settings.webhook_url:
        violations.append("staging environment requires WEBHOOK_URL to be set")
    if not settings.webhook_secret:
        violations.append("staging environment requires WEBHOOK_SECRET to be set")
    if len(settings.webhook_secret) < 16:
        violations.append(
            "staging environment requires WEBHOOK_SECRET of at least 16 characters"
        )
    if not settings.staging_id:
        violations.append(
            "staging environment requires AUSTRO_STAGING_ID to identify the "
            "deployment (e.g. staging-1)"
        )
    if settings.db_engine != "postgresql":
        violations.append(
            f"staging environment requires DB_ENGINE=postgresql, got "
            f"'{settings.db_engine}'"
        )
    if not settings.db_name:
        violations.append("staging environment requires DB_NAME to be set")
    elif settings.db_name == PRODUCTION_DB_NAME:
        violations.append(
            f"staging environment must not use the production database "
            f"'{PRODUCTION_DB_NAME}'"
        )
    elif not any(marker in settings.db_name.lower() for marker in STAGING_DB_MARKERS):
        violations.append(
            f"staging database name '{settings.db_name}' must be dedicated and "
            f"marked as staging (expected one of: {', '.join(STAGING_DB_MARKERS)})"
        )
    if not settings.db_user:
        violations.append("staging environment requires DB_USER to be set")
    if not settings.db_password:
        violations.append(
            "staging environment requires DB_PASSWORD to be set (no implicit login)"
        )
    token = (settings.bot_token or "").lower()
    if any(marker in token for marker in PLACEHOLDER_TOKEN_MARKERS):
        violations.append(
            "staging BOT_TOKEN looks like a placeholder/CI token; use the "
            "dedicated staging bot from @BotFather"
        )
    violations.extend(_transport_violations(settings.telegram_transport, "staging"))
    return violations


def validate_deployment_safety(settings) -> List[str]:
    """Return every deployment-safety violation for the configured environment."""
    environment = (settings.environment or "").strip()
    if environment == PRODUCTION:
        return production_violations(settings)
    if environment == STAGING:
        return staging_violations(settings)
    return []


__all__ = [
    "DEPLOYED_ENVIRONMENTS",
    "PLACEHOLDER_TOKEN_MARKERS",
    "PRODUCTION",
    "PRODUCTION_DB_NAME",
    "STAGING",
    "STAGING_DB_MARKERS",
    "SUPPORTED_TRANSPORTS",
    "production_violations",
    "staging_violations",
    "validate_deployment_safety",
]
