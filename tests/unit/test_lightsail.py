from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_restore_refuses_live_project_before_docker(tmp_path):
    result = subprocess.run(  # noqa: S603 - fixed local test commands
        ["/bin/bash", str(ROOT / "deploy/lightsail/restore.sh"), str(tmp_path)],
        env={**os.environ, "COMPOSE_PROJECT_NAME": "hrm-lightsail"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "scratch host" in result.stderr


def test_lightsail_compose_render_limits_private_ports_persistence_and_raw_secrets(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose not installed")
    source = ROOT / "deploy/lightsail"
    shutil.copy(source / "compose.yml", tmp_path / "compose.yml")
    (tmp_path / "runtime.env").write_text("HRM_DB_PASSWORD=fixture$literal\n")
    (tmp_path / "database.env").write_text("POSTGRES_PASSWORD=fixture\n")
    result = subprocess.run(  # noqa: S603 - fixed local test commands
        [
            shutil.which("docker"),
            "compose",
            "-f",
            str(tmp_path / "compose.yml"),
            "config",
            "--format",
            "json",
        ],
        env={**os.environ, "APP_IMAGE": "fixture/app:immutable", "APP_DOMAIN": "fixture.invalid"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    assert all(not services[name].get("ports") for name in ("db", "api", "worker"))
    assert services["frontend"]["environment"]["WEB_CONCURRENCY"] == "1"
    assert (
        services["worker"]["environment"]["HRM_DB_PASSWORD"] == "fixture$$literal"
    )  # Compose serializes literal dollars escaped for a subsequent parse
    assert services["worker"]["environment"]["HRM_ENABLE_PREPARATION"] == "false"
    assert (
        sum(
            int(services[name]["mem_limit"])
            for name in ("db", "api", "frontend", "worker", "dispatcher", "caddy")
        )
        < 3.3 * 1024**3
    )
    assert services["db"]["volumes"][0]["type"] == "volume"
    assert all(s["logging"]["options"]["max-size"] == "10m" for s in services.values())


def test_lightsail_database_file_comes_from_ssm_and_preserves_literal_values(tmp_path, monkeypatch):
    import runpy
    from unittest.mock import patch

    monkeypatch.chdir(tmp_path)
    Path("runtime.env").write_text(
        "HRM_SSM_PARAMETER_PREFIX=/project/prod\nHRM_SSM_REGION=us-west-2\n"
    )
    payload = {
        "Parameters": [
            {
                "Name": "/project/prod/database-password",
                "Type": "SecureString",
                "Value": "fixture$literal",
            }
        ]
    }
    with (
        patch("shutil.which", return_value="/usr/local/bin/aws"),
        patch("subprocess.check_output", return_value=json.dumps(payload)) as call,
    ):
        runpy.run_path(str(ROOT / "deploy/lightsail/configure_ssm.py"), run_name="__main__")
    assert Path("database.env").read_text() == "POSTGRES_PASSWORD=fixture$literal\n"
    assert Path("database.env").stat().st_mode & 0o777 == 0o600
    assert "--with-decryption" in call.call_args.args[0]
