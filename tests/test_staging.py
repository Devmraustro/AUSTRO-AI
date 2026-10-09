"""Isolated staging deployment: config safety, isolation, and rollback guards.

Fully deterministic and offline: no Docker, no network, no real database, no
real secrets. Docker-requiring validation is exercised through the plan
(`scripts/staging_validate.build_plan`) and the static isolation checks, so CI
can prove the staging procedure is internally consistent without a staging host.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import deployment_validation as dv
from scripts import staging_rollback, staging_validate

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_COMPOSE = REPO_ROOT / "docker-compose.staging.yml"
PROD_COMPOSE = REPO_ROOT / "docker-compose.yml"
STAGING_TEMPLATE = REPO_ROOT / ".env.staging.example"


@pytest.fixture(autouse=True)
def fresh_db():
    """Override the repo-wide `fresh_db` fixture for this module.

    Every test here is static or a subprocess test, so the per-test SQLite
    re-initialisation in tests/conftest.py (which dominates the suite runtime)
    would be pure overhead. Other test modules keep the original fixture.
    """
    yield None


# --------------------------------------------------------------------------- #
# deployment safety rules
# --------------------------------------------------------------------------- #
def _settings(**overrides):
    """A staging-valid configuration; override one field per test."""
    base = dict(
        environment="staging",
        log_level="INFO",
        use_local_fallback=False,
        gemini_api_key="gemini-key-for-tests",
        webhook_url="https://staging.example.com/webhook",
        webhook_secret="staging-webhook-secret",
        staging_id="staging-1",
        db_engine="postgresql",
        db_name="austro_ai_staging",
        db_user="austro_staging",
        db_password="staging-db-password",
        bot_token="123456789:AAstagingBotTokenValue0000000000",
        telegram_transport="polling",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestStagingValidationRules:
    def test_valid_staging_configuration_has_no_violations(self):
        assert dv.staging_violations(_settings()) == []

    def test_production_rules_are_unchanged(self):
        """Production keeps exactly its original five rules and nothing more."""
        ok = _settings(environment="production", log_level="WARNING",
                       staging_id="", db_engine="sqlite", db_name="",
                       db_user="", db_password="", telegram_transport="")
        assert dv.production_violations(ok) == []
        assert len(dv.production_violations(
            _settings(environment="production", log_level="INFO"))) == 1
        assert len(dv.production_violations(
            _settings(environment="production", log_level="WARNING",
                      webhook_url=""))) == 1
        assert len(dv.production_violations(
            _settings(environment="production", log_level="WARNING",
                      webhook_secret=""))) == 1
        assert len(dv.production_violations(
            _settings(environment="production", log_level="WARNING",
                      use_local_fallback=True))) == 1
        assert len(dv.production_violations(
            _settings(environment="production", log_level="WARNING",
                      gemini_api_key=""))) == 1

    def test_production_ignores_staging_only_rules(self):
        """A production deploy must not be forced to satisfy staging rules."""
        prod = _settings(environment="production", log_level="WARNING",
                         staging_id="", db_name="austro_ai", db_user="",
                         db_password="", telegram_transport="")
        assert dv.validate_deployment_safety(prod) == []

    def test_development_is_not_gated(self):
        assert dv.validate_deployment_safety(_settings(environment="development")) == []

    @pytest.mark.parametrize("field,value,needle", [
        ("use_local_fallback", True, "local fallback"),
        ("gemini_api_key", "", "GEMINI_API_KEY"),
        ("webhook_url", "", "WEBHOOK_URL"),
        ("webhook_secret", "", "WEBHOOK_SECRET"),
        ("staging_id", "", "AUSTRO_STAGING_ID"),
        ("db_engine", "sqlite", "postgresql"),
        ("db_name", "", "DB_NAME"),
        ("db_user", "", "DB_USER"),
        ("db_password", "", "DB_PASSWORD"),
    ])
    def test_staging_failures_fail_closed(self, field, value, needle):
        violations = dv.staging_violations(_settings(**{field: value}))
        assert violations, f"{field}={value!r} must be rejected in staging"
        assert any(needle in v for v in violations), violations

    def test_staging_rejects_production_database(self):
        violations = dv.staging_violations(_settings(db_name=dv.PRODUCTION_DB_NAME))
        assert any("must not use the production database" in v for v in violations)

    @pytest.mark.parametrize("db_name", ["austro_ai_live", "austro_ai_prod", "austro_ai"])
    def test_staging_requires_a_dedicated_marked_database(self, db_name):
        violations = dv.staging_violations(_settings(db_name=db_name))
        assert violations

    @pytest.mark.parametrize("db_name", ["austro_ai_staging", "uat-db", "austro_sbx",
                                          "staging_eu"])
    def test_staging_accepts_marked_databases(self, db_name):
        assert dv.staging_violations(_settings(db_name=db_name)) == []

    def test_placeholder_bot_token_rejected(self):
        violations = dv.staging_violations(
            _settings(bot_token="123456789:DummyTokenForTests"))
        assert any("placeholder" in v for v in violations)

    def test_unimplemented_webhook_transport_rejected(self):
        violations = dv.staging_violations(_settings(telegram_transport="webhook"))
        assert any("TELEGRAM_TRANSPORT" in v for v in violations)
        assert any("polling" in v for v in violations)


# --------------------------------------------------------------------------- #
# compose isolation
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def staging_compose():
    return yaml.safe_load(STAGING_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prod_compose():
    return yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))


class TestStagingComposeIsolation:
    def test_project_and_network_are_staging_scoped(self, staging_compose, prod_compose):
        assert staging_compose["name"] == "austro-staging"
        assert staging_compose["networks"]["staging"]["name"] == "austro-staging-network"
        # the production network name must not appear outside comments
        body = "\n".join(
            line for line in STAGING_COMPOSE.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        )
        assert "austro-network" not in body
        assert staging_compose["networks"]["staging"]["name"] != "austro-network"

    def test_container_names_are_staging_scoped(self, staging_compose):
        for name, service in staging_compose["services"].items():
            assert service["container_name"].startswith("austro-staging-"), name

    def test_service_names_do_not_collide_with_production(self, staging_compose,
                                                          prod_compose):
        assert set(staging_compose["services"]).isdisjoint(
            {"postgres"}), "staging must not reuse the production postgres service"
        for name, service in staging_compose["services"].items():
            assert service["container_name"] != prod_compose["services"]["app"][
                "container_name"]

    def test_app_image_and_build_are_staging_scoped(self, staging_compose):
        app = staging_compose["services"]["app"]
        assert app["image"].startswith("austro-staging-app:")
        assert app["build"]["dockerfile"] == "Dockerfile"

    def test_app_environment_is_staging(self, staging_compose):
        env = staging_compose["services"]["app"]["environment"]
        assert env["AUSTRO_ENVIRONMENT"] == "staging"
        assert env["USE_LOCAL_FALLBACK"] == "false"
        assert env["DB_ENGINE"] == "postgresql"
        assert env["TELEGRAM_TRANSPORT"] == "polling"
        for key in ("AUSTRO_STAGING_ID", "BOT_TOKEN", "GEMINI_API_KEY", "DB_NAME",
                    "DB_USER", "DB_PASSWORD", "WEBHOOK_URL", "WEBHOOK_SECRET"):
            assert key in env, key

    def test_no_production_database_fallbacks(self, staging_compose):
        """Database identity must come from .env.staging, with no hard-coded default."""
        for name, service in staging_compose["services"].items():
            for key, value in service.get("environment", {}).items():
                if key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
                    assert str(value).startswith("${DB_"), (name, key, value)
                    assert ":-" not in str(value), (name, key, value)

    def test_volumes_are_distinct_from_production(self, staging_compose, prod_compose):
        assert set(staging_compose["volumes"]) == set(prod_compose["volumes"])
        mounts = staging_compose["services"]["app"]["volumes"]
        assert "knowledge_storage:/app/knowledge_storage" in mounts
        assert "logs:/app/logs" in mounts
        # The app container must NOT mount backup archives (least privilege).
        assert not any("/app/backups" in str(m) for m in mounts)
        # compose prefixes volume names with the project name, so the project
        # name is what keeps them separate.
        assert staging_compose["name"] != prod_compose.get("name", "austro")

    def test_backup_worker_is_profile_gated_and_staging_only(self, staging_compose):
        backup = staging_compose["services"]["backup"]
        assert backup["profiles"] == ["staging-backup"]
        assert backup["image"].startswith("austro-staging-backup:")
        env = backup["environment"]
        assert env["DB_NAME"].startswith("${DB_NAME:")
        assert env["DB_HOST"].startswith("${DB_HOST:")
        assert "AUSTRO_ENVIRONMENT" not in env

    def test_local_database_is_profile_gated(self, staging_compose):
        db = staging_compose["services"]["staging-db"]
        assert db["profiles"] == ["local-staging-db"]
        assert db["image"].startswith("postgres:")
        assert db["container_name"] == "austro-staging-db"
        assert "staging" in str(db["environment"]["POSTGRES_DB"]).lower()
        assert "staging" in str(db["environment"]["POSTGRES_USER"]).lower()

    def test_app_healthcheck_uses_the_shipped_healthcheck_script(self, staging_compose,
                                                                 prod_compose):
        """Production's app service has no healthcheck; staging adds the tool the
        production image already ships (scripts/healthcheck.py)."""
        assert prod_compose["services"]["app"].get("healthcheck") is None
        healthcheck = staging_compose["services"]["app"]["healthcheck"]
        assert healthcheck["test"] == ["CMD", "python", "scripts/healthcheck.py"]
        assert (REPO_ROOT / "scripts" / "healthcheck.py").exists()


# --------------------------------------------------------------------------- #
# env template
# --------------------------------------------------------------------------- #
class TestStagingEnvTemplate:
    def test_template_exists_and_parses(self):
        values = staging_validate.read_env_file(STAGING_TEMPLATE)
        assert values, "template must define keys"
        assert values["DB_NAME"].endswith("_staging")
        assert values["USE_LOCAL_FALLBACK"] == "false"

    def test_template_contains_no_secrets(self):
        text = STAGING_TEMPLATE.read_text(encoding="utf-8")
        assert "sk-" not in text, "no provider API keys in the template"
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() in ("BOT_TOKEN", "DB_PASSWORD", "GEMINI_API_KEY",
                               "WEBHOOK_SECRET"):
                assert value.strip() == "", f"{key} must be blank in the template"

    def test_real_staging_env_file_is_git_ignored(self):
        """A committed template plus an ignored real env file."""
        proc = subprocess.run(["git", "check-ignore", ".env.staging"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True)
        assert proc.returncode == 0, ".env.staging must be git-ignored"
        proc = subprocess.run(["git", "check-ignore", "-q", ".env.staging.example"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True)
        assert proc.returncode != 0, ".env.staging.example must be trackable"

    def test_template_documents_the_isolation_rules(self):
        text = STAGING_TEMPLATE.read_text(encoding="utf-8")
        for needle in ("NEVER the production token", "different from production",
                       "staging-marked"):
            assert needle in text, needle


# --------------------------------------------------------------------------- #
# validation harness
# --------------------------------------------------------------------------- #
class TestValidationHarness:
    def test_plan_covers_every_required_check(self):
        keys = [step.key for step in staging_validate.build_plan()]
        assert keys == [
            "compose_config", "config_isolation", "image_build", "cold_start",
            "healthcheck", "smoke_test", "telegram_startup", "database_isolation",
            "storage_logs", "restart_recovery",
        ]

    def test_every_docker_command_is_staging_scoped(self):
        for step in staging_validate.build_plan():
            for argv in step.commands:
                if argv[0] != "docker":
                    continue
                text = " ".join(argv)
                if "-f docker-compose.staging.yml" not in text:
                    # the only other docker command reads the production file
                    assert "docker-compose.yml config -q" in text, text
                assert "-p austro-staging" in text or "config -q" in text, text

    def test_plan_never_contains_a_secret(self):
        for step in staging_validate.build_plan():
            for argv in step.commands:
                text = " ".join(argv)
                if "docker-compose.staging.yml" in text:
                    assert "--env-file" in text, text
                for forbidden in ("BOT_TOKEN=", "DB_PASSWORD=", "GEMINI_API_KEY=",
                                  "WEBHOOK_SECRET="):
                    assert forbidden not in text, text

    def test_plan_mode_runs_nothing(self, capsys):
        assert staging_validate.main([]) == 0
        out = capsys.readouterr().out
        assert "nothing below is executed" in out
        assert "docker compose -f docker-compose.staging.yml" in out

    def test_plan_json_is_machine_readable(self, capsys):
        import json
        assert staging_validate.main(["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["key"] == "compose_config"

    def test_isolation_rejects_a_missing_env_file(self):
        result = staging_validate.check_config_isolation(".env.staging.absent")
        assert result.status == staging_validate.BLOCKED
        assert "not present" in result.detail

    def test_isolation_rejects_unset_required_keys(self, tmp_path, monkeypatch):
        env = tmp_path / ".env.staging"
        env.write_text("DB_NAME=austro_ai_staging\nBOT_TOKEN=\n", encoding="utf-8")
        monkeypatch.setattr(staging_validate, "REPO_ROOT", tmp_path)
        result = staging_validate.check_config_isolation(".env.staging")
        assert result.status == staging_validate.FAIL
        assert "BOT_TOKEN" in result.detail

    def test_isolation_rejects_a_production_database_name(self, tmp_path, monkeypatch):
        env = tmp_path / ".env.staging"
        env.write_text("DB_NAME=austro_ai\n", encoding="utf-8")
        monkeypatch.setattr(staging_validate, "REPO_ROOT", tmp_path)
        result = staging_validate.check_config_isolation(".env.staging")
        assert result.status == staging_validate.FAIL
        assert "not staging-marked" in result.detail

    def test_isolation_rejects_credentials_shared_with_production(self, tmp_path,
                                                                  monkeypatch):
        secret = "123456789:AAtheSameTokenAsProduction0000"
        (tmp_path / ".env").write_text(f"BOT_TOKEN={secret}\n", encoding="utf-8")
        (tmp_path / ".env.staging").write_text(
            f"DB_NAME=austro_ai_staging\nBOT_TOKEN={secret}\n", encoding="utf-8")
        monkeypatch.setattr(staging_validate, "REPO_ROOT", tmp_path)
        result = staging_validate.check_config_isolation(".env.staging")
        assert result.status == staging_validate.FAIL
        assert "BOT_TOKEN" in result.detail
        assert secret not in result.detail, "the shared secret must never be printed"

    def test_isolation_blocks_when_production_env_is_absent(self, tmp_path, monkeypatch):
        (tmp_path / ".env.staging").write_text(
            "DB_NAME=austro_ai_staging\nBOT_TOKEN=123456789:AAdistinct000\n",
            encoding="utf-8")
        monkeypatch.setattr(staging_validate, "REPO_ROOT", tmp_path)
        result = staging_validate.check_config_isolation(".env.staging")
        assert result.status == staging_validate.BLOCKED
        assert "cannot be compared" in result.detail

    def test_isolation_passes_for_fully_separated_credentials(self, tmp_path,
                                                              monkeypatch):
        (tmp_path / ".env").write_text(
            "BOT_TOKEN=111:AAprod\nDB_NAME=austro_ai\n", encoding="utf-8")
        (tmp_path / ".env.staging").write_text(
            "DB_NAME=austro_ai_staging\nBOT_TOKEN=222:AAstaging\n"
            "DB_USER=austro_staging\nDB_PASSWORD=staging\n"
            "WEBHOOK_SECRET=staging-secret-value\n"
            "WEBHOOK_URL=https://staging.example.com/hook\n",
            encoding="utf-8")
        monkeypatch.setattr(staging_validate, "REPO_ROOT", tmp_path)
        result = staging_validate.check_config_isolation(".env.staging")
        assert result.status == staging_validate.PASS

    def test_fingerprint_never_reveals_a_secret(self):
        secret = "super-secret-value"
        printed = staging_validate.fingerprint(secret)
        assert secret not in printed
        assert "len=18" in printed and printed.startswith("<set")
        assert staging_validate.fingerprint("") == "<empty>"

    def test_execute_without_docker_reports_blocked_not_passed(self, monkeypatch):
        """No Docker must never look like a successful staging deployment."""
        monkeypatch.setattr(staging_validate, "docker_available", lambda: False)
        results = staging_validate.validate(".env.staging", execute=True)
        assert results
        assert all(r.status in (staging_validate.BLOCKED, staging_validate.FAIL)
                   for r in results)
        assert not any(r.status == staging_validate.PASS for r in results)

    def test_storage_marker_round_trip(self, tmp_path):
        knowledge, logs = tmp_path / "knowledge", tmp_path / "logs"
        assert staging_validate.probe_storage("run1", str(knowledge), str(logs)) == 0
        assert (knowledge / "staging_marker.txt").read_text(encoding="utf-8") == "run1"
        assert staging_validate.verify_storage_marker("run1", str(knowledge),
                                                      str(logs)) == 0
        assert staging_validate.verify_storage_marker("other", str(knowledge),
                                                      str(logs)) == 1
        # a lost volume is detected
        assert staging_validate.verify_storage_marker("run1", str(tmp_path / "gone"),
                                                      str(logs)) == 1


# --------------------------------------------------------------------------- #
# rollback guards
# --------------------------------------------------------------------------- #
class TestRollbackGuards:
    def test_refuses_when_environment_is_not_staging(self):
        with pytest.raises(staging_rollback.RollbackRefused) as exc:
            staging_rollback.assert_staging_only(".env.staging", None, "production")
        assert "never rolls back production" in str(exc.value)

    def test_refuses_a_production_image(self):
        with pytest.raises(staging_rollback.RollbackRefused) as exc:
            staging_rollback.assert_staging_only(".env.staging", "austro-ai-app:main",
                                                 "staging")
        assert "production images cannot be rolled into staging" in str(exc.value)

    def test_refuses_an_unidentifiable_image(self):
        for tag in ("local", "latest", "", "main", "dev", "WIP-3"):
            with pytest.raises(staging_rollback.RollbackRefused):
                staging_rollback.assert_verified_image(f"austro-staging-app:{tag}",
                                                       allow_unverified=True)

    def test_accepts_an_identifiable_verified_image(self):
        staging_rollback.assert_verified_image("austro-staging-app:5f0d604", False)
        staging_rollback.assert_verified_image("austro-staging-app:ci-42", False)
        staging_rollback.assert_verified_image("austro-staging-app:verified-9", False)

    def test_short_unverified_tag_needs_the_override(self):
        with pytest.raises(staging_rollback.RollbackRefused):
            staging_rollback.assert_verified_image("austro-staging-app:abc", False)
        staging_rollback.assert_verified_image("austro-staging-app:abc", True)

    def test_plan_is_staging_scoped_and_never_restores(self):
        joined = "\n".join(
            " ".join(argv) for argv in
            staging_rollback.build_plan("austro-staging-app:5f0d604", ".env.staging")
        )
        assert joined, "rollback plan must not be empty"
        assert "-f docker-compose.staging.yml" in joined
        assert "-p austro-staging" in joined
        for forbidden in ("docker-compose.yml", "pg_restore", "DROP DATABASE",
                          "pg_restore_drill", "docker compose down"):
            assert forbidden not in joined, forbidden

    def test_health_is_verified_after_the_rollback(self):
        plan = staging_rollback.build_plan("austro-staging-app:5f0d604", ".env.staging")
        joined = "\n".join(" ".join(c) for c in plan)
        assert "healthcheck.py" in joined
        assert "Health.Status" in joined
        # the container is replaced first, and only then is health verified
        replaced = next(i for i, c in enumerate(plan) if "up" in c and "app" in c)
        inspected = next(i for i, c in enumerate(plan) if "Health.Status" in " ".join(c))
        checked = next(i for i, c in enumerate(plan) if "healthcheck.py" in " ".join(c))
        assert replaced < inspected < checked

    def test_cli_refuses_restore(self, capsys):
        assert staging_rollback.main(["--allow-restore"]) == 2
        assert "never restores a database" in capsys.readouterr().out

    def test_cli_refuses_production_rollback(self, monkeypatch, capsys):
        monkeypatch.setenv("AUSTRO_ENVIRONMENT", "production")
        code = staging_rollback.main(["--image", "austro-staging-app:5f0d604"])
        assert code == 2
        assert "REFUSED" in capsys.readouterr().out

    def test_cli_refuses_a_production_image(self, monkeypatch, capsys):
        monkeypatch.setenv("AUSTRO_ENVIRONMENT", "staging")
        code = staging_rollback.main(["--image", "austro-ai-app:main"])
        assert code == 2
        assert "production images cannot be rolled into staging" in capsys.readouterr().out

    def test_cli_plan_mode_is_read_only(self, monkeypatch, capsys):
        monkeypatch.setenv("AUSTRO_ENVIRONMENT", "staging")
        assert staging_rollback.main(["--plan", "--image",
                                      "austro-staging-app:5f0d604"]) == 0
        out = capsys.readouterr().out
        assert "nothing executed" in out
        assert "forward-only" in out


# --------------------------------------------------------------------------- #
# smoke test is reusable for staging
# --------------------------------------------------------------------------- #
class TestSmokeTestReusability:
    def test_smoke_test_defaults_to_production(self):
        source = (REPO_ROOT / "scripts" / "smoke_test.py").read_text(encoding="utf-8")
        assert 'os.environ.setdefault("AUSTRO_ENVIRONMENT", "production")' in source

    def test_smoke_test_honours_an_explicit_staging_environment(self):
        source = (REPO_ROOT / "scripts" / "smoke_test.py").read_text(encoding="utf-8")
        assert "expected_environment = os.environ" in source
        assert "PRODUCTION COLD-START" not in source

    def test_smoke_test_enforces_staging_rules_in_staging_mode(self, tmp_path):
        """Running the battery as `staging` must apply the staging rules.

        The full battery can only pass against a real staging PostgreSQL (step 6
        of scripts/staging_validate.py runs it inside the container), so the
        offline assertion here is the fail-closed behaviour: an unsafe staging
        configuration is rejected by the smoke path itself.
        """
        import os
        env = dict(os.environ)
        env.update({
            "AUSTRO_ENVIRONMENT": "staging",
            "AUSTRO_STAGING_ID": "staging-1",
            "AUSTRO_LOG_LEVEL": "INFO",
            "USE_LOCAL_FALLBACK": "false",
            "DB_ENGINE": "sqlite",
            "DB_PATH": str(tmp_path / "smoke_staging.db"),
            "BOT_TOKEN": "123456789:AAstagingSmokeToken0000000000",
            "GEMINI_API_KEY": "staging-gemini-key-for-tests",
            "WEBHOOK_URL": "https://staging.example.com/webhook",
            "WEBHOOK_SECRET": "staging-webhook-secret",
            "TELEGRAM_TRANSPORT": "polling",
            "PYTHONIOENCODING": "utf-8",
        })
        proc = subprocess.run([sys.executable, "scripts/smoke_test.py"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True,
                              env=env, timeout=600)
        output = proc.stdout + proc.stderr
        # sqlite is not a valid staging engine, so the battery must fail closed
        assert proc.returncode != 0
        assert "requires DB_ENGINE=postgresql" in output
        # the environment really was staging, not the production default
        assert "staging configuration validation" in output
        assert "PRODUCTION COLD-START" not in output

    def test_smoke_test_passes_for_production_by_default(self, tmp_path):
        """The default (production) battery still passes: behaviour unchanged."""
        import os
        env = dict(os.environ)
        env.pop("AUSTRO_ENVIRONMENT", None)
        env.update({
            "AUSTRO_LOG_LEVEL": "WARNING",
            "USE_LOCAL_FALLBACK": "false",
            "DB_ENGINE": "sqlite",
            "DB_PATH": str(tmp_path / "smoke_prod.db"),
            "GEMINI_API_KEY": "smoke-gemini-key-0000000000",
            "WEBHOOK_URL": "https://smoke.example.com/webhook",
            "WEBHOOK_SECRET": "smoke-webhook-secret-0000000",
            "PYTHONIOENCODING": "utf-8",
        })
        env.setdefault("BOT_TOKEN", "123456789:ABCdefgh-DummyToken")
        proc = subprocess.run([sys.executable, "scripts/smoke_test.py"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True,
                              env=env, timeout=600)
        output = proc.stdout + proc.stderr
        assert "FAILURES PRESENT" not in output, output
        assert "SMOKE RESULT: ALL 8 COLD-START CHECKS PASSED (production)" in output
