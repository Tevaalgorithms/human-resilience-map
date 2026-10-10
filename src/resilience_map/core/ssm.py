"""Opt-in, read-only SSM configuration. Values never appear in errors or logs."""

from __future__ import annotations

import re
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

PLACEHOLDER = "REPLACE_IN_AWS_CONSOLE"
PARAMETERS = {
    "db_password": "database-password",
    "session_secret": "session-secret",
    "cognito_client_secret": "cognito-client-secret",
    "OPENAI_API_KEY": "openai-api-key",
    "census_api_key": "census-api-key",
}


def load_secrets(config: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Called only when HRM_SECRET_SOURCE=ssm; no writes or fallback to .env secrets."""
    prefix = str(config.get("ssm_parameter_prefix", "/human-resilience-map/local"))
    role = str(config.get("runtime_role", "api"))
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", prefix):
        raise ValueError("Use a valid absolute SSM parameter prefix without a trailing slash.")
    if role not in {"api", "frontend", "worker", "dispatcher", "migrate", "ingest"}:
        raise ValueError("Select a supported runtime role for SSM secret loading.")
    fields = ["db_password", "session_secret"]
    if role == "frontend" and config.get("auth_mode", "local") == "cognito":
        fields.append("cognito_client_secret")
    if role in {"api", "worker"} and config.get("llm_provider", "openai") == "openai":
        fields.append("OPENAI_API_KEY")
    if config.get("ssm_require_census_key", False) in {True, "true", "True", "1"} or (
        role == "api" and config.get("enable_preparation", False) in {True, "true", "True", "1"}
    ):
        fields.append("census_api_key")
    names = {f"{prefix}/{PARAMETERS[field]}": field for field in fields}
    try:
        ssm = client or boto3.client(
            "ssm",
            region_name=config.get("ssm_region") or config.get("aws_region", "us-east-1"),
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
        )
        response = ssm.get_parameters(Names=list(names), WithDecryption=True)
    except (BotoCoreError, ClientError):
        raise ValueError(
            "SSM secret loading failed. Check the region, AWS identity, parameter paths and IAM/KMS read permissions."
        ) from None
    values = {p["Name"]: p for p in response.get("Parameters", []) if p.get("Name") in names}
    result: dict[str, Any] = {
        "database_url": None,
        "db_password": "",
        "session_secret": "",
        "cognito_client_secret": "",
        "OPENAI_API_KEY": "",
        "census_api_key": "",
    }
    for name, field in names.items():
        parameter = values.get(name, {})
        value = parameter.get("Value", "")
        if (
            parameter.get("Type") != "SecureString"
            or not isinstance(value, str)
            or not value.strip()
            or value == PLACEHOLDER
        ):
            raise ValueError(
                f"Set a real SecureString value for SSM parameter {name} before starting this service."
            )
        result[field] = value
    return result
