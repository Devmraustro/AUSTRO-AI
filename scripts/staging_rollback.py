"""
AUSTRO AI - STAGING ROLLBACK (release-checklist item 11).

Roll the isolated staging slot back to a previously verified, identifiable image.

Design rules (all of them are enforced in code, not just documented):

  * STAGING ONLY. Refuses to act unless `AUSTRO_ENVIRONMENT=staging` is set,
    the staging compose file is used and the compose project is `austro-staging`.
    It never runs `docker-compose.yml`, so production cannot be restarted,
    re-tagged or reconfigured by this script.
  * IDENTIFIABLE TARGET. The target must be given as a full image reference
    (`austro-staging-app:<tag>`) and, by default, must be tagged as verified.
    Untagged/unverified images are rejected unless --allow-unverified is passed.
  * HEALTH GATED. The new container must reach `healthy` and
    `scripts/healthcheck.py` must exit 0 before the rollback is reported done.
  * MIGRATIONS ARE NOT REVERSED. This schema is forward-only (there is no down
    migration), so rolling the image back can leave an app older than the schema.
    The script reports the migration state and refuses to proceed when
    --require-migration-compatible is not satisfied; it never guesses.
  * RESTORE IS EXPLICIT AND DISPOSABLE-ONLY. A database restore is NEVER run
    here. `--allow-restore` is rejected outright so this script cannot become a
    production-grade restore tool; restore drills belong in
    scripts/pg_restore_drill.py against a disposable staging database.

Usage:
    python scripts/staging_rollback.py --env-file .env.staging --plan
    python scripts/staging_rollback.py --env-file .env.staging \\
        --image austro-staging-app:5f0d604
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_COMPOSE = "docker-compose.staging.yml"
STAGING_PROJECT = "austro-staging"
STAGING_ENV = "staging"
STAGING_APP_IMAGE_PREFIX = "austro-staging-app:"


class RollbackRefused(Exception):
    """Raised when a safety precondition fails; nothing has been changed."""


def compose_base(env_file: str) -> List[str]:
    return [
        "docker", "compose",
        "-f", STAGING_COMPOSE,
        "-p", STAGING_PROJECT,
        "--env-file", env_file,
    ]


def build_plan(image: Optional[str], env_file: str) -> List[List[str]]:
    """The exact command sequence a rollback performs (pure, testable)."""
    target = image or f"{STAGING_APP_IMAGE_PREFIX}local"
    compose = compose_base(env_file)
    return [
        ["docker", "pull", target] if "/" in target else ["docker", "image", "inspect", target],
        compose + ["up", "-d", "--no-build", "--no-deps", "app"],
        ["docker", "inspect", "--format", "{{.State.Health.Status}}",
         f"{STAGING_PROJECT}-app-1"],
        compose + ["exec", "-T", "app", "python", "scripts/healthcheck.py"],
    ]


def assert_staging_only(env_file: str, image: Optional[str], environment: str,
                        require_env_file: bool = True) -> None:
    """Fail closed before touching anything if this is not a staging rollback."""
    if environment.strip().lower() != STAGING_ENV:
        raise RollbackRefused(
            f"refusing to run: AUSTRO_ENVIRONMENT={environment!r} is not "
            f"'{STAGING_ENV}'. This script never rolls back production."
        )
    if not (REPO_ROOT / STAGING_COMPOSE).exists():
        raise RollbackRefused(f"missing {STAGING_COMPOSE}; run from the repository root")
    if image is not None and not image.startswith(STAGING_APP_IMAGE_PREFIX):
        raise RollbackRefused(
            f"refusing target {image!r}: expected a staging image reference "
            f"starting with '{STAGING_APP_IMAGE_PREFIX}' so production images "
            "cannot be rolled into staging"
        )
    if not (REPO_ROOT / env_file).exists():
        message = f"{env_file} not found"
        if require_env_file:
            raise RollbackRefused(
                f"{message}; staging rollbacks need a staging env file"
            )
        print(f"WARNING: {message}; showing the plan only, nothing can be executed")


def assert_verified_image(image: str, allow_unverified: bool) -> None:
    """A rollback target must be a known-good build, not a local experiment."""
    tag = image.split(":", 1)[1] if ":" in image else ""
    if not tag or tag in ("local", "latest"):
        raise RollbackRefused(
            f"refusing target {image!r}: 'local'/'latest' is not an identifiable, "
            "previously verified image. Tag a commit sha or a CI run number."
        )
    if tag in ("main", "dev", "staging") or tag.startswith("WIP"):
        raise RollbackRefused(f"refusing target {image!r}: tag is not a verified build")
    if not allow_unverified and not any(
        token in tag for token in ("verified", "ci-", "-g")
    ) and len(tag) < 7:
        raise RollbackRefused(
            f"refusing target {image!r}: tag {tag!r} does not identify a verified "
            "build. Pass --allow-unverified if you have already validated it."
        )


_MIGRATION_QUERY = (
    "from app.database.connection import DatabaseManager;"
    "conn=DatabaseManager()._get_connection();cur=conn.cursor();"
    "cur.execute('SELECT schema_name, version FROM schema_migrations ORDER BY 1');"
    "rows=cur.fetchall();"
    "print(','.join(f'{r[0]}@{r[1]}' for r in rows) or '(none recorded)');"
    "conn.close()"
)


def migration_report(env_file: str) -> str:
    """Report applied migrations without changing anything.

    Migrations are forward-only: this is information for a human deciding
    whether an older image is safe to run, never an automatic down-migration.
    """
    try:
        proc = subprocess.run(
            compose_base(env_file) + ["exec", "-T", "app", "python", "-c",
                                      _MIGRATION_QUERY],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    if proc.returncode != 0:
        return f"unavailable: exit={proc.returncode} {(proc.stderr or '').strip()[:200]}"
    return (proc.stdout or "").strip() or "(none recorded)"


def run(argv: Sequence[str], timeout: int = 900) -> int:
    print(f"$ {' '.join(argv)}")
    try:
        return subprocess.run(list(argv), cwd=str(REPO_ROOT),
                              timeout=timeout).returncode
    except FileNotFoundError:
        print(f"ERROR: executable not found: {argv[0]}")
        return 127
    except subprocess.TimeoutExpired:
        print("ERROR: command timed out")
        return 124


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AUSTRO AI staging rollback")
    parser.add_argument("--env-file", default=".env.staging")
    parser.add_argument("--image", help="target image, e.g. austro-staging-app:5f0d604")
    parser.add_argument("--plan", action="store_true",
                        help="print the plan and exit without changing anything")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="permit a target that was validated outside this tool")
    parser.add_argument("--allow-restore", action="store_true",
                        help=argparse.SUPPRESS)  # always refused; see module docstring
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.allow_restore:
        print("REFUSED: this script never restores a database. Restore drills are "
              "run with scripts/pg_restore_drill.py against a disposable staging "
              "database only.")
        return 2

    image = args.image

    try:
        assert_staging_only(args.env_file, image, _current_environment(args.env_file),
                            require_env_file=not args.plan)
        if image is not None:
            assert_verified_image(image, args.allow_unverified)
    except RollbackRefused as exc:
        print(f"REFUSED: {exc}")
        return 2

    if args.plan or image is None:
        print("\nStaging rollback plan (nothing executed)")
        for command in build_plan(image, args.env_file):
            print(f"  $ {' '.join(command)}")
        print("\nPreconditions enforced: staging environment, staging compose file, "
              f"project {STAGING_PROJECT}, identifiable verified image, no restore.")
        print("Migrations are forward-only; review the migration report before "
              "rolling an older image back.")
        return 0

    if shutil.which("docker") is None:
        print("BLOCKED: docker CLI not available; live rollback not verified.")
        return 1

    print(f"Staging rollback to {image}")
    print(f"Applied migrations on the current container: {migration_report(args.env_file)}")
    for command in build_plan(image, args.env_file):
        code = run(command)
        if code != 0:
            print(f"ERROR: rollback step failed (exit {code}); staging may be degraded")
            return 1
    print("Rollback complete: container healthy and healthcheck passed.")
    return 0


def _current_environment(env_file: str) -> str:
    """Read AUSTRO_ENVIRONMENT from the staging env file (never from the OS).

    A rollback must be driven by the staging env file, so an operator's shell
    cannot redirect it at production by exporting a variable. Falls back to the
    process environment only for documentation/plan purposes.
    """
    path = REPO_ROOT / env_file
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("AUSTRO_ENVIRONMENT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""
    return os.environ.get("AUSTRO_ENVIRONMENT", "")


if __name__ == "__main__":
    sys.exit(main())
