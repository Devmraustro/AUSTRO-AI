"""Deployment contract for encrypted backups (compose, image, git hygiene).

These are static checks of the committed files. They run offline and fail the
build if someone re-introduces: an app container with access to backup
archives, an unencrypted backup worker, a key passed through the environment,
a writable plaintext location on the backup volume, or an image that cannot
import `cryptography`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _compose(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prod():
    return _compose("docker-compose.yml")


@pytest.fixture(scope="module")
def staging():
    return _compose("docker-compose.staging.yml")


@pytest.mark.parametrize("fixture_name,secret", [
    ("prod", "backup_encryption_key"),
    ("staging", "staging_backup_encryption_key"),
])
def test_backup_worker_enables_encryption_from_a_read_only_secret(
        request, fixture_name, secret):
    doc = request.getfixturevalue(fixture_name)
    backup = doc["services"]["backup"]
    env = backup["environment"]
    assert str(env["BACKUP_ENCRYPTION_ENABLED"]).lower() == "true"
    key_path = env["BACKUP_ENCRYPTION_KEY_FILE"]
    assert key_path == f"/run/secrets/{secret}"
    assert secret in backup["secrets"]
    # the key itself must never be an environment value
    for value in env.values():
        assert "BEGIN" not in str(value)
    assert doc["secrets"][secret]["file"]  # host path defined, never inline content
    assert "content" not in doc["secrets"][secret]


@pytest.mark.parametrize("fixture_name", ["prod", "staging"])
def test_backup_plaintext_work_dir_is_tmpfs_not_the_backup_volume(request, fixture_name):
    doc = request.getfixturevalue(fixture_name)
    backup = doc["services"]["backup"]
    assert backup["environment"]["BACKUP_WORKDIR"] == "/var/tmp/austro-backup-work"
    mounts = backup["volumes"]
    tmpfs = [m for m in mounts if isinstance(m, dict) and m.get("type") == "tmpfs"]
    assert tmpfs and tmpfs[0]["target"] == "/var/tmp/austro-backup-work"
    # the only persistent mount is the archive directory
    persistent = [m for m in mounts if not (isinstance(m, dict) and m.get("type") == "tmpfs")]
    assert persistent == ["backups:/app/backups"]
    assert backup.get("read_only") is True


@pytest.mark.parametrize("fixture_name", ["prod", "staging"])
def test_application_container_cannot_read_backup_archives(request, fixture_name):
    doc = request.getfixturevalue(fixture_name)
    app_mounts = [str(m) for m in doc["services"]["app"].get("volumes", [])]
    assert not any("/app/backups" in m or m.startswith("backups:") for m in app_mounts)


def test_backup_image_installs_and_asserts_cryptography():
    dockerfile = (REPO_ROOT / "Dockerfile.backup").read_text(encoding="utf-8")
    reqs = (REPO_ROOT / "requirements-backup.txt").read_text(encoding="utf-8")
    assert "cryptography>=" in reqs
    assert "requirements-backup.txt" in dockerfile
    assert "import cryptography" in dockerfile
    assert "AESGCM" in dockerfile


def test_backup_image_runs_as_fixed_non_root_user():
    dockerfile = (REPO_ROOT / "Dockerfile.backup").read_text(encoding="utf-8")
    assert "--uid 10001" in dockerfile
    assert "USER austro" in dockerfile


def test_key_material_directories_are_git_ignored_and_not_in_build_context():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "secrets/" in gitignore
    assert "secrets/" in dockerignore
    assert "*.key" in dockerignore


def test_env_templates_document_backup_encryption_without_real_values():
    for name in (".env.example", ".env.staging.example"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "BACKUP_ENCRYPTION" in text or "BACKUP_ENCRYPTION_KEY_FILE_HOST" in text \
            or "STAGING_BACKUP_ENCRYPTION_KEY_FILE" in text, name
        for line in text.splitlines():
            if line.startswith("BACKUP_ENCRYPTION_KEY"):
                assert line.split("=", 1)[1].strip() in ("", "./secrets/backup_encryption_key",
                                                         "./secrets/staging_backup_encryption_key"), line


def test_restore_drill_has_no_default_target_in_the_compose_worker(prod):
    env = prod["services"]["backup"]["environment"]
    assert not any("RESTORE" in k for k in env), "restore is an explicit operator action"


def test_git_tracks_no_key_or_dump_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:  # pragma: no cover - not a git checkout (sdist)
        pytest.skip("not a git checkout")
    bad = [f for f in out.stdout.splitlines()
           if f.endswith((".key", ".sql", ".sql.gz", ".dump")) or f.startswith("secrets/")]
    assert not bad, bad


def test_deprecated_backup_wrapper_fails_closed_without_key(tmp_path):
    """scripts/backup.sh must never write a plaintext archive.

    With no encryption key and BACKUP_ENCRYPTION_ENABLED unset in the caller's
    environment, the wrapper forces encryption on and refuses before any
    database contact or archive write.
    """
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in ("BACKUP_ENCRYPTION_ENABLED", "BACKUP_ENCRYPTION_KEY_FILE")}
    env.update({"DB_HOST": "127.0.0.1", "DB_PORT": "1"})
    outdir = tmp_path / "backups"
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "backup.sh"), str(outdir)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "BACKUP_ENCRYPTION_KEY_FILE is not configured" in (result.stdout + result.stderr)
    archives = list(outdir.glob("*")) if outdir.exists() else []
    assert archives == []
