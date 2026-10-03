"""AUSTRO AI - Docker build-context and image-content regression tests.

The audit found no tracked `.dockerignore` while `Dockerfile` used
`COPY . .`. That shipped every local `.env`, SQLite database, `backups/*.sql`
dump (full memory + knowledge content), log file and `.git` directory into the
build context and into image layers. `.gitignore` does not apply to the build
context, so these tests pin the actual control.

Run:  python -m pytest tests/test_docker_context.py -v
"""

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "Dockerfile"

# Paths that must never enter the build context. Each entry is (label, pattern)
# where pattern is matched against the path relative to the repository root.
MUST_EXCLUDE = [
    (".env", ".env"),
    (".env.staging", ".env.staging"),
    ("live .env.production", ".env.production"),
    ("sqlite database", "austro.db"),
    ("sqlite wal", "austro.db-wal"),
    ("postgres-style dump", "backups/austro_20260101.sql"),
    ("compressed dump", "backups/austro_20260101.sql.gz"),
    ("log file", "logs/bot.log"),
    ("runtime uploads", "knowledge_storage/source1/data.txt"),
    ("git dir", ".git/config"),
    ("virtualenv", ".venv/lib/python3.12/site-packages/x.py"),
    ("pycache", "app/__pycache__/x.pyc"),
    ("pytest cache", ".pytest_cache/v/cache/lastfailed"),
    ("coverage", ".coverage"),
    ("private key", "certs/server.key"),
    ("crt", "certs/server.crt"),
]

# Files the image must still contain. Dropping any of these breaks startup.
MUST_INCLUDE = [
    "app",
    "scripts",
    "main.py",
    "handlers.py",
    "config.py",
    "database.py",
    "reminder_scheduler.py",
    "redaction.py",
    "requirements.txt",
    "docker-entrypoint.sh",
]

# Never allowed in the image, whatever the copy rules.
FORBIDDEN_IN_IMAGE = [
    ".env",
    ".env.staging",
    ".env.production",
    "backups",
    "logs",
    "knowledge_storage",
    ".git",
]


def _dockerignore_patterns():
    """Return (negated, [patterns]) parsed from .dockerignore."""
    if not DOCKERIGNORE.exists():
        return set(), []
    raw = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    ]
    negated = set()
    patterns = []
    for line in raw:
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negated.add(line[1:].strip())
        else:
            patterns.append(line)
    return {f"!{n}" for n in negated}, patterns


def _matches(pattern: str, path: str) -> bool:
    """Match a .dockerignore pattern against a relative path.

    Implements the Docker semantics this repo relies on: a bare name matches any
    path component, and a trailing-slash prefix matches a directory subtree.
    """
    pattern = pattern.rstrip("/")
    if not pattern:
        return False
    if pattern == path:
        return True
    # A glob with no slash matches any single path component by fnmatch.
    if "/" not in pattern and ("*" in pattern or "?" in pattern or "[" in pattern):
        return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))
    # A bare pattern (no slash) matches any single path component.
    if "/" not in pattern:
        return any(part == pattern for part in path.split("/"))
    # A directory pattern matches the subtree.
    return path.startswith(pattern + "/") or f"/{pattern}/" in f"/{path}"


def _is_excluded(path: str) -> bool:
    """Return True if .dockerignore excludes `path`, honouring negations."""
    negated, patterns = _dockerignore_patterns()
    excluded = False
    for pattern in patterns:
        if _matches(pattern, path):
            excluded = True
    for entry in negated:
        if _matches(entry.lstrip("!"), path):
            excluded = False
    return excluded


def test_dockerignore_exists():
    assert DOCKERIGNORE.exists(), ".dockerignore must exist to control the build context"
    assert DOCKERIGNORE.stat().st_size > 0, ".dockerignore must not be empty"


def test_secrets_and_runtime_state_are_excluded():
    """Every local secret / DB / backup / log path must be excluded."""
    failures = []
    for label, path in MUST_EXCLUDE:
        if not _is_excluded(path):
            failures.append(f"{label} ({path}) is NOT excluded")
    assert not failures, "build-context leaks:\n  " + "\n  ".join(failures)


def test_runtime_files_are_still_included():
    """Excluding secrets must not remove files the container needs."""
    failures = []
    for path in MUST_INCLUDE:
        if _is_excluded(path.rstrip("/")):
            failures.append(f"{path} is excluded but required at runtime")
    assert not failures, "missing runtime paths:\n  " + "\n  ".join(failures)


def test_env_examples_stay_trackable():
    """`.env.example` must reach the image/build so operators have a template."""
    _negated, patterns = _dockerignore_patterns()
    assert ".env.*" in patterns, ".env.* should be excluded by default"
    assert "!.env.example" in _negated, ".env.example must be re-included"
    assert not _is_excluded(".env.example")


def test_dockerfile_does_not_copy_everything():
    """`COPY . .` defeats .dockerignore scoping; require explicit copies."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^COPY\s", line, re.IGNORECASE)
    ]
    assert copy_lines, "Dockerfile must COPY something"
    broad = [
        line for line in copy_lines
        if line.split()[1:2] == ["."] or line.split()[1:2] == ["./"]
    ]
    assert not broad, (
        "Dockerfile must not use `COPY . .` / `COPY ./ .`: every excluded "
        f"pattern then only hides the file from a later layer. Found: {broad}"
    )


def test_dockerfile_copies_every_runtime_path():
    """The explicit allow-list must actually cover the runtime surface."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    copied = set()
    for match in re.finditer(r"^\s*COPY\s+(.*)$", text, re.IGNORECASE | re.MULTILINE):
        parts = match.group(1).split()
        # Skip flag-prefixed forms (COPY --chmod=0755 src dst).
        parts = [p for p in parts if not p.startswith("--")]
        for token in parts[:-1]:
            copied.add(token.lstrip("./").rstrip("/"))
    missing = [p for p in MUST_INCLUDE if p not in copied]
    assert not missing, f"runtime paths not COPYed by Dockerfile: {missing}"


def test_no_forbidden_path_in_dockerfile_copies():
    """No COPY instruction may reintroduce an excluded path."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    bad = []
    for forbidden in FORBIDDEN_IN_IMAGE:
        pattern = re.compile(
            rf"^\s*COPY\s+.*\b{re.escape(forbidden)}\b", re.IGNORECASE | re.MULTILINE
        )
        if pattern.search(text):
            bad.append(forbidden)
    assert not bad, f"Dockerfile COPYs excluded paths: {bad}"


def test_dockerignore_covers_gitignore_secrets():
    """Any secret-looking path in .gitignore must also be in .dockerignore."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    _negated, patterns = _dockerignore_patterns()
    secrety = [
        line.strip()
        for line in gitignore.splitlines()
        if line.strip()
        and not line.startswith("#")
        and not line.startswith("!")
        and re.search(r"\.env|\.db$|\.db-|sql|key|secret|log|backup|venv|__pycache__",
                      line, re.IGNORECASE)
    ]
    missing = []
    for pattern in secrety:
        bare = pattern.rstrip("/")
        if bare in (".env.example", ".env.staging.example"):
            continue
        if not any(_matches(p, bare) or _matches(p, f"{bare}/x") for p in patterns):
            missing.append(pattern)
    assert not missing, (
        ".gitignore excludes these but .dockerignore does not: "
        + ", ".join(missing)
    )


def _docker_available() -> bool:
    """True only if a usable docker CLI is on PATH."""
    try:
        return subprocess.run(
            ["docker", "--version"], capture_output=True, check=False
        ).returncode == 0
    except (FileNotFoundError, OSError):
        return False


@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_built_image_contains_no_secrets():  # pragma: no cover - needs Docker
    """Build the image and assert no secret path exists inside it."""
    subprocess.run(
        ["docker", "build", "-t", "austro-audit-test", "."],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    listing = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         "austro-audit-test", "-c", "find /app -maxdepth 2"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for forbidden in FORBIDDEN_IN_IMAGE:
        assert f"/app/{forbidden}" not in listing, (
            f"{forbidden} present in image"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))