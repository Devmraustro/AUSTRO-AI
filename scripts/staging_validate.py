"""
AUSTRO AI - staging deployment validation (release-checklist item 11).

A safe, reproducible checklist for validating the ISOLATED staging slot:

  1. compose config      - both compose files parse and resolve
  2. config isolation    - staging secrets differ from production secrets, and
                           the staging database is staging-marked
  3. image build         - the app image builds
  4. cold start          - container starts, entrypoint applies migrations
  5. healthcheck         - scripts/healthcheck.py prints HEALTHY, exit 0
  6. smoke test          - the full offline smoke battery in staging mode
  7. telegram startup    - transport + dedicated bot identity (no secrets shown)
  8. database isolation  - the container talks to the staging DB, not prod
  9. storage and logs    - persistent volumes writable and survive a restart
 10. restart recovery    - restart keeps the schema and returns to HEALTHY

Two modes:
  * default          : print the plan (every command, nothing executed)
  * --execute        : run the steps and report PASS / FAIL / BLOCKED
  * --json           : machine-readable results

SECURITY RULES BUILT INTO THIS SCRIPT
  * No secret is ever printed. Values are only ever reported as
    "<set,len=N,sha256=xxxxxxxx>" so drift can be compared without leaking.
  * No command line ever contains a secret: compose reads `.env.staging`.
  * Nothing here can touch production: every docker command is scoped to
    `-f docker-compose.staging.yml -p austro-staging`, and the production
    compose file is only ever *read* (never run).
  * No destructive database command is issued. Restore drills live in
    scripts/staging_rollback.py and target a disposable staging database only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_COMPOSE = "docker-compose.staging.yml"
PROD_COMPOSE = "docker-compose.yml"
STAGING_PROJECT = "austro-staging"
STAGING_ENV_FILE = ".env.staging"
PROD_ENV_FILE = ".env"

#: Values that must never be identical between staging and production.
ISOLATED_KEYS = (
    "BOT_TOKEN",
    "WEBHOOK_SECRET",
    "WEBHOOK_URL",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "GEMINI_API_KEY",
)

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIP = "SKIP"


# --------------------------------------------------------------------------- #
# secret-safe helpers
# --------------------------------------------------------------------------- #
def fingerprint(value: str) -> str:
    """Describe a secret without revealing it (never logged in the clear)."""
    if not value:
        return "<empty>"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"<set,len={len(value)},sha256={digest}>"


def read_env_file(path: Path) -> Dict[str, str]:
    """Minimal KEY=VALUE reader (no interpolation, no logging of values)."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def docker_available() -> bool:
    return shutil.which("docker") is not None


def compose_base(env_file: str) -> List[str]:
    """Command prefix that scopes every docker call to the staging project."""
    return [
        "docker", "compose",
        "-f", STAGING_COMPOSE,
        "-p", STAGING_PROJECT,
        "--env-file", env_file,
    ]


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    """One validation step: what it proves and the exact commands to run."""

    key: str
    title: str
    commands: List[List[str]] = field(default_factory=list)
    needs_docker: bool = True
    note: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "commands": [" ".join(c) for c in self.commands],
            "needs_docker": self.needs_docker,
            "note": self.note,
        }


def build_plan(env_file: str = STAGING_ENV_FILE) -> List[Step]:
    """The ordered validation plan. Pure function -> deterministic and testable."""
    compose = compose_base(env_file)
    return [
        Step(
            key="compose_config",
            title="Compose configuration resolves (staging + production parse)",
            commands=[
                ["docker", "compose", "-f", STAGING_COMPOSE, "--env-file", env_file, "config", "-q"],
                ["docker", "compose", "-f", PROD_COMPOSE, "config", "-q"],
            ],
            note="Production is only read/parsed here, never started or mutated.",
        ),
        Step(
            key="config_isolation",
            title="Staging secrets and database are distinct from production",
            commands=[],
            needs_docker=False,
            note="Static: compares .env.staging with .env without printing values.",
        ),
        Step(
            key="image_build",
            title="Staging app image builds",
            commands=[compose + ["build", "app"]],
        ),
        Step(
            key="cold_start",
            title="Cold start applies startup migrations",
            commands=[compose + ["up", "-d", "app"]],
            note="docker-entrypoint.sh waits for the DB, then applies migrations.",
        ),
        Step(
            key="healthcheck",
            title="Healthcheck reports HEALTHY (exit 0)",
            commands=[compose + ["exec", "-T", "app", "python", "scripts/healthcheck.py"]],
        ),
        Step(
            key="smoke_test",
            title="Full smoke test passes in staging mode",
            commands=[compose + ["exec", "-T", "-e", "AUSTRO_ENVIRONMENT=staging",
                                 "app", "python", "scripts/smoke_test.py"]],
        ),
        Step(
            key="telegram_startup",
            title="Telegram startup: dedicated bot, supported transport",
            commands=[compose + ["exec", "-T", "app", "python",
                                 "scripts/staging_validate.py", "--check-telegram"]],
            note="Validates transport + that the configured token is a dedicated, "
                 "non-placeholder staging token, offline so the token is never put "
                 "in a URL or log. Live getMe confirmation is a human step "
                 "(TELEGRAM_E2E_PROCEDURE.md).",
        ),
        Step(
            key="database_isolation",
            title="Container is bound to the staging database, not production",
            commands=[compose + ["exec", "-T", "app", "python",
                                 "scripts/staging_validate.py", "--print-db-identity"]],
        ),
        Step(
            key="storage_logs",
            title="Persistent storage and logs are writable",
            commands=[compose + ["exec", "-T", "app", "python",
                                 "scripts/staging_validate.py", "--probe-storage",
                                 "--marker", "run1"]],
            note="Writes a marker into knowledge_storage and logs; the next step "
                 "re-reads it after a restart.",
        ),
        Step(
            key="restart_recovery",
            title="Restart recovery: storage intact, schema intact, HEALTHY again",
            commands=[
                compose + ["restart", "app"],
                compose + ["exec", "-T", "app", "python",
                           "scripts/staging_validate.py", "--verify-storage-marker",
                           "run1"],
                compose + ["exec", "-T", "app", "python",
                           "scripts/staging_validate.py", "--print-db-identity"],
                compose + ["exec", "-T", "app", "python", "scripts/healthcheck.py"],
            ],
            note="Re-reads the persistent marker and the database identity to prove "
                 "volumes and schema survived a restart, then re-checks health.",
        ),
    ]


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    key: str
    status: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"key": self.key, "status": self.status, "detail": self.detail}


class Runner:
    """Thin subprocess wrapper; never echoes secrets (they are not in argv)."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(self, argv: Sequence[str], timeout: int = 900) -> Tuple[int, str]:
        if self.dry_run:
            return 0, "(dry run) not executed"
        try:
            proc = subprocess.run(
                list(argv), cwd=str(REPO_ROOT), capture_output=True, text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            return 127, f"command not found: {exc.filename}"
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output.strip()


def check_compose_config(runner: Runner, env_file: str) -> Result:
    for argv in (
        ["docker", "compose", "-f", STAGING_COMPOSE, "--env-file", env_file, "config", "-q"],
        ["docker", "compose", "-f", PROD_COMPOSE, "config", "-q"],
    ):
        code, out = runner.run(argv, timeout=180)
        if code != 0:
            status = BLOCKED if "not found" in out else FAIL
            return Result("compose_config", status, f"{' '.join(argv[3:])} -> {out[:200]}")
    return Result("compose_config", PASS, "staging and production compose files resolve")


def check_config_isolation(env_file: str) -> Result:
    staging_path = REPO_ROOT / env_file
    if not staging_path.exists():
        return Result("config_isolation", BLOCKED,
                      f"{env_file} not present; copy .env.staging.example first")
    staging = read_env_file(staging_path)
    prod_path = REPO_ROOT / PROD_ENV_FILE
    missing = [k for k in ISOLATED_KEYS if k in staging and not staging[k]]
    if missing:
        return Result("config_isolation", FAIL,
                      "unset required staging keys: " + ", ".join(sorted(missing)))

    db_name = staging.get("DB_NAME", "")
    if "staging" not in db_name.lower():
        return Result("config_isolation", FAIL,
                      f"DB_NAME {fingerprint(db_name)} is not staging-marked")

    if not prod_path.exists():
        return Result("config_isolation", BLOCKED,
                      f"{PROD_ENV_FILE} not present on this host, so production "
                      "values cannot be compared; staging keys are set and "
                      "staging-marked")
    prod = read_env_file(prod_path)
    shared = [
        key for key in ISOLATED_KEYS
        if staging.get(key) and prod.get(key) and staging[key] == prod[key]
    ]
    if shared:
        return Result("config_isolation", FAIL,
                      "staging reuses production value(s) for: " + ", ".join(shared))
    compared = [k for k in ISOLATED_KEYS if prod.get(k) and staging.get(k)]
    return Result("config_isolation", PASS,
                  f"{len(compared)} credential(s) differ from production; "
                  "no value printed")


def check_db_identity() -> int:
    """Print the database identity the running container is bound to (no secrets)."""
    from app.config.settings import settings
    from app.config.deployment_validation import PRODUCTION_DB_NAME, STAGING

    print(f"environment={settings.environment}")
    print(f"db_engine={settings.db_engine}")
    print(f"db_host={settings.db_host}")
    print(f"db_name={settings.db_name}")
    print(f"db_user={settings.db_user}")
    print(f"db_password={fingerprint(settings.db_password)}")
    if settings.environment != STAGING:
        print("ERROR: this helper must only run in the staging container")
        return 1
    if settings.db_name == PRODUCTION_DB_NAME:
        print("ERROR: staging container is bound to the production database")
        return 1
    if "staging" not in settings.db_name.lower():
        print("ERROR: staging database name is not staging-marked")
        return 1
    return 0


def check_telegram_startup() -> int:
    """Verify the Telegram startup contract inside the staging container.

    Deliberately offline: the token is never placed in a URL, argv or log, so a
    live ``getMe`` call cannot leak it. Confirming the staging bot really answers
    is a human step (TELEGRAM_E2E_PROCEDURE.md).
    """
    from app.config.settings import settings
    from app.config.deployment_validation import (
        PLACEHOLDER_TOKEN_MARKERS,
        STAGING,
        SUPPORTED_TRANSPORTS,
    )

    print(f"environment={settings.environment}")
    print(f"transport={settings.telegram_transport}")
    print(f"bot_token={fingerprint(settings.bot_token)}")
    print(f"webhook_url_host={urlsplit(settings.webhook_url or '').netloc or '<empty>'}")
    print(f"webhook_secret={fingerprint(settings.webhook_secret)}")
    errors: List[str] = []
    if settings.environment != STAGING:
        errors.append("this helper must only run in the staging container")
    if settings.telegram_transport not in SUPPORTED_TRANSPORTS:
        errors.append(f"unsupported transport {settings.telegram_transport!r}")
    token = settings.bot_token or ""
    if not token or ":" not in token or len(token) < 20:
        errors.append("staging bot token looks unset or malformed")
    if any(marker in token.lower() for marker in PLACEHOLDER_TOKEN_MARKERS):
        errors.append("staging bot token is still a placeholder/CI token")
    if len(settings.webhook_secret or "") < 16:
        errors.append("staging webhook secret is shorter than 16 characters")
    for problem in errors:
        print(f"ERROR: {problem}")
    if not errors:
        print("note: transport=polling, so WEBHOOK_URL/WEBHOOK_SECRET are "
              "configuration only; the runtime long-polls")
        print("telegram startup contract OK (offline; live getMe is a human step)")
    return 1 if errors else 0


def probe_storage(marker: str, knowledge_dir: Optional[str] = None,
                  logs_dir: Optional[str] = None) -> int:
    """Write marker files into the persistent volumes (proves writability)."""
    from app.config.settings import settings

    knowledge = Path(knowledge_dir or settings.knowledge_storage_path)
    logs = Path(logs_dir or settings.logs_path)
    for directory, name in ((knowledge, "staging_marker.txt"),
                            (logs, "staging_log_marker.txt")):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(marker, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot write {name} in {directory}: {exc}")
            return 1
    print(f"wrote marker {fingerprint(marker)} to {knowledge} and {logs}")
    return 0


def verify_storage_marker(marker: str, knowledge_dir: Optional[str] = None,
                          logs_dir: Optional[str] = None) -> int:
    """Re-read marker files after a restart (proves the volumes persisted)."""
    from app.config.settings import settings

    knowledge = Path(knowledge_dir or settings.knowledge_storage_path)
    logs = Path(logs_dir or settings.logs_path)
    for path in (knowledge / "staging_marker.txt", logs / "staging_log_marker.txt"):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: persistent marker missing after restart: {path} ({exc})")
            return 1
        if content != marker:
            print(f"ERROR: persistent marker changed across restart: {path}")
            return 1
    print("persistent storage and logs survived the restart")
    return 0


def wait_for_health(runner: Runner, env_file: str, timeout: int = 180) -> Result:
    """Poll `docker inspect` until the staging app container reports healthy."""
    if runner.dry_run:
        return Result("cold_start", SKIP, "dry run")
    container = f"{STAGING_PROJECT}-app-1"
    deadline = time.time() + timeout
    last = "no status yet"
    while time.time() < deadline:
        code, out = runner.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            timeout=60,
        )
        last = out.strip() or f"exit={code}"
        if last == "healthy":
            return Result("cold_start", PASS, f"{container} is healthy")
        if last in ("unhealthy", "exited", "dead"):
            return Result("cold_start", FAIL, f"{container} reported {last}")
        time.sleep(5)
    return Result("cold_start", BLOCKED,
                  f"{container} did not become healthy within {timeout}s (last={last})")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def validate(env_file: str, execute: bool, runner: Optional[Runner] = None) -> List[Result]:
    runner = runner or Runner(dry_run=not execute)
    plan = {step.key: step for step in build_plan(env_file)}
    results: List[Result] = []
    has_docker = docker_available()

    results.append(check_config_isolation(env_file))

    if not has_docker:
        # Never report BLOCKED as success: a missing Docker CLI means the live
        # staging deployment is simply unverified.
        for step in plan.values():
            if step.needs_docker:
                results.append(Result(step.key, BLOCKED,
                                      "docker CLI not available in this environment; "
                                      "live staging deployment not verified"))
        return results

    compose = compose_base(env_file)
    results.append(check_compose_config(runner, env_file))

    code, out = runner.run(compose + ["build", "app"], timeout=1800)
    results.append(Result("image_build", PASS if code == 0 else FAIL,
                          "image built" if code == 0 else out[:300]))

    code, out = runner.run(compose + ["up", "-d", "app"], timeout=900)
    if code != 0:
        results.append(Result("cold_start", FAIL, out[:300]))
        return results
    health = wait_for_health(runner, env_file)
    results.append(health)
    if health.status != PASS:
        return results

    for key, title in (
        ("healthcheck", "healthcheck"),
        ("smoke_test", "smoke test"),
        ("telegram_startup", "telegram startup"),
        ("database_isolation", "database identity"),
    ):
        code, out = runner.run(plan[key].commands[0], timeout=900)
        if code == 0 and "ERROR:" in out:
            results.append(Result(key, FAIL, out[:300]))
        else:
            results.append(Result(key, PASS if code == 0 else FAIL,
                                  f"{title} ok" if code == 0 else out[:300]))

    for argv in plan["restart_recovery"].commands:
        code, out = runner.run(argv, timeout=900)
        if code != 0:
            results.append(Result("restart_recovery", FAIL, out[:300]))
            break
    else:
        results.append(Result("restart_recovery", PASS,
                              "restarted, schema intact, HEALTHY again"))
    return results


def print_plan(env_file: str) -> None:
    print("AUSTRO AI staging validation plan")
    print(f"  compose : {STAGING_COMPOSE}")
    print(f"  project : {STAGING_PROJECT}")
    print(f"  env file: {env_file}")
    print("  note    : nothing below is executed in plan mode\n")
    for index, step in enumerate(build_plan(env_file), start=1):
        print(f"{index:2}. {step.title}  [{step.key}]")
        if step.note:
            print(f"    note: {step.note}")
        for argv in step.commands:
            print(f"    $ {' '.join(argv)}")
        if not step.commands:
            print("    (static check, no command)")
        print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AUSTRO AI staging deployment validation")
    parser.add_argument("--env-file", default=STAGING_ENV_FILE,
                        help=f"staging env file (default: {STAGING_ENV_FILE})")
    parser.add_argument("--execute", action="store_true",
                        help="actually run the steps (default: print the plan only)")
    parser.add_argument("--static", action="store_true",
                        help="run only the static config-isolation check (no Docker); "
                             "used by CI, where no staging slot exists")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--print-db-identity", action="store_true",
                        help="print the database identity of the running container "
                             "and exit (no secrets); staging only")
    parser.add_argument("--probe-storage", action="store_true",
                        help="write a marker into the persistent storage/log volumes")
    parser.add_argument("--check-telegram", action="store_true",
                        help="verify the offline Telegram startup contract "
                             "(transport + dedicated staging token; no secrets)")
    parser.add_argument("--verify-storage-marker", metavar="MARKER",
                        help="verify the persistent marker written earlier")
    parser.add_argument("--marker", default="run1",
                        help="marker value for --probe-storage (default: run1)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.print_db_identity:
        return check_db_identity()
    if args.check_telegram:
        return check_telegram_startup()
    if args.probe_storage:
        return probe_storage(args.marker)
    if args.verify_storage_marker is not None:
        return verify_storage_marker(args.verify_storage_marker)
    if args.static:
        result = check_config_isolation(args.env_file)
        print(f"[{result.status:>7}] {result.key}: {result.detail}")
        if result.status == BLOCKED:
            print("BLOCKED: this is a static check only; it is not evidence of a "
                  "successful staging deployment")
        return 1 if result.status == FAIL else 0

    if not args.execute:
        if args.json:
            print(json.dumps([s.as_dict() for s in build_plan(args.env_file)], indent=2))
        else:
            print_plan(args.env_file)
        return 0

    results = validate(args.env_file, execute=True)
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print("\nStaging validation results")
        print("-" * 72)
        for result in results:
            print(f"[{result.status:>7}] {result.key}: {result.detail}")
        print("-" * 72)
        failed = [r for r in results if r.status == FAIL]
        blocked = [r for r in results if r.status == BLOCKED]
        print(f"{len(results)} checks: "
              f"{sum(1 for r in results if r.status == PASS)} passed, "
              f"{len(failed)} failed, {len(blocked)} blocked")
        if blocked:
            print("BLOCKED checks require Docker and a real staging slot; "
                  "they are NOT evidence of a successful deployment.")
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
