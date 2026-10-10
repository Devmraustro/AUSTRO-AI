"""AUSTRO AI - restore safety rules (pure, offline, no database access).

Every rule in this module is evaluated BEFORE any destructive database
operation (DROP / CREATE DATABASE, psql restore). Nothing here opens a
connection, so the rules can be unit-tested without PostgreSQL.

Rules enforced by `scripts/pg_restore_drill.py`:

  1. A restore target must be named explicitly (`RESTORE_TARGET_DB`). There is
     no default: the application database is never a fallback.
  2. The target must look like a disposable restore database and must differ
     from the application database (`DB_NAME`), the backup source database and
     the PostgreSQL system databases. Concretely:
       a. Production-like names are refused first, regardless of case or
          separators. The name is lowercased and split on every non-alphanumeric
          character (`_`, `-`, `.`, space and so on) into tokens. Refused when any
          token is `prod`, `production`, `live`, `primary` or `master`, or starts
          with `prod` (so `prodrestore` is refused). This is intentionally broad:
          a legitimate name such as `products_restore` is also refused.
       b. Live-database names are refused. Compared with all separators removed
          and lowercased, the name must not equal the application DB, the source
          DB, or the fixed live names `austroai` and `austro`. So `Austro-AI`
          matches `austro_ai`.
       c. The name must be a valid lowercase identifier (`a-z`, `0-9`, `_`; 3 to
          63 characters, starting with a letter).
       d. It must not be a PostgreSQL system database.
       e. It must contain `restore`.
     `austro_ai_restore_drill` passes every rule.
  3. Destructive authorization is explicit: `RESTORE_CONFIRM_DESTRUCTIVE` must
     equal `DROP-AND-RESTORE:<target>` exactly. A bare `yes` is not enough.
  4. Administrative credentials are explicit: `RESTORE_ADMIN_USER` and
     `RESTORE_ADMIN_PASSWORD` are required. The restore never assumes a
     `postgres` superuser account.
  5. The archive is authenticated, decrypted (when encrypted), decompressed and
     checksum-verified end to end BEFORE the target is touched. See
     `verify_backup_before_restore`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Mapping, Optional

RESTORE_TARGET_ENV = "RESTORE_TARGET_DB"
RESTORE_CONFIRM_ENV = "RESTORE_CONFIRM_DESTRUCTIVE"
RESTORE_ADMIN_USER_ENV = "RESTORE_ADMIN_USER"
RESTORE_ADMIN_PASSWORD_ENV = "RESTORE_ADMIN_PASSWORD"
CONFIRM_PREFIX = "DROP-AND-RESTORE:"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})
_RESTORE_MARKER = "restore"
# Fixed live-database names that are refused whatever the configuration says.
_LIVE_DATABASE_NAMES = frozenset({"austroai", "austro"})
# Whole-token markers of a production or live database.
_PRODUCTION_TOKENS = frozenset({"prod", "production", "live", "primary", "master"})
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


class RestoreRefused(RuntimeError):
    """A restore request violated a safety rule. Raised before any DB change."""


class IntegrityError(RuntimeError):
    """The archive failed authentication, decryption, decompression or checksum."""


def required_confirmation(target: str) -> str:
    """The exact token an operator must supply to authorise a destructive restore."""
    return f"{CONFIRM_PREFIX}{target}"


def _collapsed(name: str) -> str:
    """Lowercase with every separator removed: `Austro-AI` -> `austroai`."""
    return _TOKEN_SPLIT_RE.sub("", name.lower())


def _looks_production(name: str) -> bool:
    """True when the name carries a production or live marker (rule 2a)."""
    tokens = [t for t in _TOKEN_SPLIT_RE.split(name.lower()) if t]
    return any(t in _PRODUCTION_TOKENS or t.startswith("prod") for t in tokens)


def validate_restore_target(
    target: Optional[str],
    *,
    app_db: Optional[str],
    source_db: Optional[str] = None,
) -> str:
    """Return the validated target name or raise RestoreRefused."""
    if not target or not target.strip():
        raise RestoreRefused(
            f"{RESTORE_TARGET_ENV} is required: restores never default to the "
            "application database"
        )
    name = target.strip()
    if _looks_production(name):
        raise RestoreRefused(
            f"restore target {name!r} looks like a production or live database "
            "(production/prod/live/primary/master token); refusing"
        )
    if app_db and name == app_db:
        raise RestoreRefused(
            f"restore target {name!r} is the application database (DB_NAME); refusing"
        )
    if source_db and name == source_db:
        raise RestoreRefused(
            f"restore target {name!r} is the backup source database; refusing"
        )
    live_names = set(_LIVE_DATABASE_NAMES)
    if app_db:
        live_names.add(_collapsed(app_db))
    if source_db:
        live_names.add(_collapsed(source_db))
    if _collapsed(name) in live_names:
        raise RestoreRefused(
            f"restore target {name!r} matches a live database name; refusing"
        )
    if not _NAME_RE.match(name):
        raise RestoreRefused(
            f"restore target {name!r} is not a valid lowercase database name"
        )
    if name in _SYSTEM_DATABASES:
        raise RestoreRefused(f"restore target {name!r} is a PostgreSQL system database")
    if _RESTORE_MARKER not in name:
        raise RestoreRefused(
            f"restore target {name!r} must contain '{_RESTORE_MARKER}' to mark it "
            "as a disposable database"
        )
    return name


def validate_destructive_authorization(target: str, env: Mapping[str, str]) -> None:
    """Require the exact, target-specific confirmation token."""
    supplied = (env.get(RESTORE_CONFIRM_ENV) or "").strip()
    expected = required_confirmation(target)
    if supplied != expected:
        raise RestoreRefused(
            f"destructive restore not authorised: set {RESTORE_CONFIRM_ENV}="
            f"{expected} to proceed"
        )


def resolve_admin_credentials(env: Mapping[str, str]) -> tuple:
    """Return (admin_user, admin_password). Both are mandatory; no defaults."""
    user = (env.get(RESTORE_ADMIN_USER_ENV) or "").strip()
    password = env.get(RESTORE_ADMIN_PASSWORD_ENV) or ""
    if not user:
        raise RestoreRefused(
            f"{RESTORE_ADMIN_USER_ENV} is required: the restore uses explicit "
            "administrative credentials"
        )
    if not password:
        raise RestoreRefused(f"{RESTORE_ADMIN_PASSWORD_ENV} is required")
    return user, password


def validate_restore_request(
    env: Mapping[str, str],
    *,
    app_db: Optional[str],
    source_db: Optional[str] = None,
) -> tuple:
    """Run every pre-restore rule that needs no I/O.

    Returns (target, admin_user, admin_password). Raises RestoreRefused.
    """
    target = validate_restore_target(
        env.get(RESTORE_TARGET_ENV), app_db=app_db, source_db=source_db
    )
    validate_destructive_authorization(target, env)
    admin_user, admin_password = resolve_admin_credentials(env)
    return target, admin_user, admin_password


def verify_backup_before_restore(
    backup: Path,
    manifest: Mapping,
    *,
    archive_is_encrypted: Callable[[Path], bool],
    file_sha256: Callable[[Path], str],
    open_sql: Callable[[Path], object],
    chunk: int = 1024 * 1024,
) -> None:
    """Authenticate the archive completely. Raises IntegrityError on any problem.

    Performs NO database operation. Streams the full decrypt+decompress path so
    a wrong key or any tampered byte is detected here, not during the restore.
    """
    if not backup.is_file():
        raise IntegrityError(f"backup archive not found: {backup.name}")
    expected_content = manifest.get("checksum_sha256")
    expected_archive = manifest.get("archive_sha256")
    if not expected_content or not expected_archive:
        raise IntegrityError("manifest lacks checksum_sha256/archive_sha256")

    encrypted = archive_is_encrypted(backup)
    declared = bool((manifest.get("encryption") or {}).get("enabled"))
    if declared and not encrypted:
        raise IntegrityError("manifest declares encryption but archive is not encrypted")
    if encrypted and not declared:
        raise IntegrityError("archive is encrypted but manifest does not declare it")

    if file_sha256(backup) != expected_archive:
        raise IntegrityError("archive sha256 does not match manifest")

    digest = hashlib.sha256()
    try:
        with open_sql(backup) as handle:
            for block in iter(lambda: handle.read(chunk), b""):
                digest.update(block)
    except ValueError as exc:
        raise IntegrityError(str(exc)) from exc
    if digest.hexdigest() != expected_content:
        raise IntegrityError("decompressed SQL sha256 does not match manifest")
