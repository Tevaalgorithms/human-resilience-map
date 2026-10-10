"""Verify runtime secrets survive Compose interpolation without entering release metadata."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from unittest.mock import patch


def test_runtime_secret_rendering(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "deploy/poc/configure.py"
    monkeypatch.chdir(tmp_path)
    release = {
        "region": "us-east-1",
        "ssm_parameter_prefix": "/hrm-poc",
        "app_image": "registry/app:sha",
        "postgis_image": "registry/db:sha",
        "domain": "resilience.example.com",
        "config": {"HRM_AUTH_MODE": "cognito"},
    }
    Path("release.json").write_text(json.dumps(release))
    secret = {
        "Parameters": [
            {"Name": "/hrm-poc/database-password", "Type": "SecureString", "Value": 'a$b"c'}
        ]
    }
    with patch("subprocess.check_output", return_value=json.dumps(secret)) as call:
        runpy.run_path(str(script))
    runtime = json.loads(Path("runtime.json").read_text())
    assert runtime["services"]["db"]["environment"]["POSTGRES_PASSWORD"] == 'a$$b"c'
    assert runtime["services"]["worker"]["environment"]["HRM_AUTH_MODE"] == "cognito"
    assert runtime["services"]["worker"]["environment"]["HRM_SECRET_SOURCE"] == "ssm"
    assert "HRM_DB_PASSWORD" not in runtime["services"]["worker"]["environment"]
    assert 'a$b"c' not in Path("release.json").read_text()
    assert "ssm" in call.call_args.args[0] and "--with-decryption" in call.call_args.args[0]
    assert Path("runtime.json").stat().st_mode & 0o777 == 0o600
    assert call.call_args.args[0][0] == "/usr/local/bin/aws"


def test_poc_refuses_unfilled_ssm_database_parameter(tmp_path, monkeypatch):
    import pytest

    script = Path(__file__).resolve().parents[2] / "deploy/poc/configure.py"
    monkeypatch.chdir(tmp_path)
    Path("release.json").write_text(
        json.dumps({"region": "us-east-1", "ssm_parameter_prefix": "/hrm-poc"})
    )
    response = {
        "Parameters": [
            {
                "Name": "/hrm-poc/database-password",
                "Type": "SecureString",
                "Value": "REPLACE_IN_AWS_CONSOLE",
            }
        ]
    }
    with (
        patch("subprocess.check_output", return_value=json.dumps(response)),
        pytest.raises(SystemExit, match="Populate"),
    ):
        runpy.run_path(str(script))
    assert not Path("runtime.json").exists()
