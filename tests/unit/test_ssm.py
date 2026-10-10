"""SSM secret-source security contracts. All AWS responses are mocked."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from resilience_map.core.config import Settings
from resilience_map.core.ssm import PARAMETERS, PLACEHOLDER, load_secrets

PREFIX = "/human-resilience-map/local"


def client(values=None, parameter_type="SecureString"):
    values = values or dict.fromkeys(PARAMETERS.values(), "ssm-fixture-value")

    def get_parameters(**kwargs):
        assert kwargs["WithDecryption"] is True
        return {
            "Parameters": [
                {"Name": name, "Type": parameter_type, "Value": values[name.rsplit("/", 1)[1]]}
                for name in kwargs["Names"]
                if name.rsplit("/", 1)[1] in values
            ]
        }

    return Mock(get_parameters=Mock(side_effect=get_parameters))


def test_ssm_overrides_env_and_explicit_credentials_without_url_bypass(monkeypatch, tmp_path):
    ssm = client()
    monkeypatch.setattr("resilience_map.core.ssm.boto3.client", lambda *args, **kwargs: ssm)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=env-fixture-key\nHRM_DB_PASSWORD=env-fixture-db\nHRM_DATABASE_URL=postgresql://unsafe-fixture\nHRM_SESSION_SECRET=env-fixture-session\n"
    )
    settings = Settings(
        _env_file=tmp_path / ".env",
        secret_source="ssm",
        llm_provider="openai",
        runtime_role="api",
        ssm_region="us-west-2",
        OPENAI_API_KEY="explicit-fixture-key",
        database_url="explicit-fixture-url",
    )
    assert settings.openai_api_key.get_secret_value() == "ssm-fixture-value"
    assert settings.db_password == "ssm-fixture-value"
    assert settings.session_secret == "ssm-fixture-value"
    assert settings.database_url is None
    assert "ssm-fixture-value" not in repr(settings)
    assert set(ssm.get_parameters.call_args.kwargs["Names"]) == {
        PREFIX + "/" + name for name in ["database-password", "session-secret", "openai-api-key"]
    }


@pytest.mark.parametrize(
    "role,extra",
    [
        ("frontend", ["cognito-client-secret"]),
        ("api", ["openai-api-key"]),
        ("worker", ["openai-api-key"]),
        ("dispatcher", []),
        ("migrate", []),
        ("ingest", []),
    ],
)
def test_service_least_privilege(role, extra):
    ssm = client()
    values = load_secrets({"runtime_role": role, "auth_mode": "cognito"}, ssm)
    assert set(ssm.get_parameters.call_args.kwargs["Names"]) == {
        PREFIX + "/" + name for name in ["database-password", "session-secret", *extra]
    }
    if role not in {"api", "worker"}:
        assert values["OPENAI_API_KEY"] == ""


@pytest.mark.parametrize("value", [PLACEHOLDER, "", "  "])
def test_placeholder_and_empty_values_fail_closed(value):
    with pytest.raises(ValueError, match="Set a real SecureString"):
        load_secrets({}, client({"database-password": value}))


def test_missing_or_plain_string_parameter_fails_closed():
    with pytest.raises(ValueError, match="session-secret"):
        load_secrets({}, client({"database-password": "fixture-value"}))
    with pytest.raises(ValueError, match="SecureString"):
        load_secrets({}, client(parameter_type="String"))


@pytest.mark.parametrize(
    "error",
    [
        NoCredentialsError(),
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "private-fixture-value"}},
            "GetParameters",
        ),
    ],
)
def test_aws_errors_are_sanitized(error, monkeypatch):
    monkeypatch.setattr("resilience_map.core.ssm.boto3.client", Mock(side_effect=error))
    with pytest.raises(ValueError) as exc:
        load_secrets({})
    assert "private-fixture-value" not in str(exc.value)
    assert "SSM secret loading failed" in str(exc.value)


def test_environment_mode_never_calls_aws(monkeypatch):
    aws = Mock(side_effect=AssertionError("AWS must not be accessed"))
    monkeypatch.setattr("resilience_map.core.ssm.boto3.client", aws)
    settings = Settings(_env_file=None, secret_source="environment", OPENAI_API_KEY="env-fixture")
    assert settings.openai_api_key.get_secret_value() == "env-fixture"
    aws.assert_not_called()


def test_invalid_source_config_fails_before_aws():
    ssm = client()
    for config in [{"ssm_parameter_prefix": "/bad/path/"}, {"runtime_role": "unknown"}]:
        with pytest.raises(ValueError):
            load_secrets(config, ssm)
    ssm.get_parameters.assert_not_called()


def test_api_fetches_census_only_when_preparation_enabled():
    ssm = client()
    load_secrets({"enable_preparation": "true"}, ssm)
    assert PREFIX + "/census-api-key" in ssm.get_parameters.call_args.kwargs["Names"]


def test_production_rejects_environment_secrets_without_aws_call(monkeypatch):
    aws = Mock(side_effect=AssertionError("AWS must not be accessed"))
    monkeypatch.setattr("resilience_map.core.ssm.boto3.client", aws)
    with pytest.raises(ValueError, match="HRM_SECRET_SOURCE"):
        Settings(
            _env_file=None,
            environment="prod",
            auth_mode="cognito",
            secret_source="environment",
            session_secret="fixture-session-value-that-is-long-enough",
        )
    aws.assert_not_called()


def test_production_accepts_configured_ssm(monkeypatch):
    ssm = client(
        dict.fromkeys(PARAMETERS.values(), "fixture-strong-ssm-secret-that-is-long-enough")
    )
    monkeypatch.setattr("resilience_map.core.ssm.boto3.client", lambda *args, **kwargs: ssm)
    settings = Settings(
        _env_file=None,
        environment="prod",
        auth_mode="cognito",
        secret_source="ssm",
        ssm_parameter_prefix="/project/prod",
        runtime_role="frontend",
    )
    assert settings.secret_source == "ssm"
    assert settings.openai_api_key.get_secret_value() == ""


def test_no_hardcoded_database_or_session_defaults():
    settings = Settings(_env_file=None, database_url=None, db_password="", session_secret="")
    assert settings.session_secret == ""
    with pytest.raises(ValueError, match="HRM_DB_PASSWORD"):
        _ = settings.sqlalchemy_url


def test_census_cli_can_request_key_without_blocking_file_ingestion():
    ssm = client()
    load_secrets({"runtime_role": "ingest", "ssm_require_census_key": "true"}, ssm)
    assert PREFIX + "/census-api-key" in ssm.get_parameters.call_args.kwargs["Names"]
