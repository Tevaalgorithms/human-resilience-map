"""Typed, environment-driven settings. No secrets are stored in code."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="HRM_", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["local", "test", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"

    # SSM is explicit until operators have replaced Terraform's placeholders.
    secret_source: Literal["environment", "ssm"] = "environment"  # noqa: S105 - source selector, not a credential
    ssm_parameter_prefix: str = Field(
        default="/human-resilience-map/local", pattern=r"^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$"
    )
    ssm_region: str | None = None
    runtime_role: Literal["api", "frontend", "worker", "dispatcher", "migrate", "ingest"] = "api"
    enable_preparation: bool = False
    ssm_require_census_key: bool = False
    census_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("CENSUS_API_KEY", "HRM_CENSUS_API_KEY", "census_api_key"),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[Any, ...]:
        def ssm_source() -> dict[str, Any]:
            config = {**dotenv_settings(), **env_settings(), **init_settings()}
            if config.get("secret_source", "environment") != "ssm":
                return {}
            from resilience_map.core.ssm import load_secrets

            return load_secrets(config)

        # SSM takes precedence even over explicitly supplied secret values in SSM mode.
        return ssm_source, init_settings, env_settings, dotenv_settings, file_secret_settings

    # --- Database: either a full URL (local) or parts (SSM supplies the production password) ---
    database_url: str | None = Field(default=None, repr=False)
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "resilience"
    db_user: str = "resilience"
    db_password: str = Field(default="", repr=False)
    db_sslmode: str = "prefer"

    # --- Auth ---
    auth_mode: Literal["local", "cognito"] = "local"
    local_dev_user: str = "dev-user"
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_client_secret: str = Field(default="", repr=False)
    cognito_domain: str = ""  # e.g. https://my-pool.auth.us-east-1.amazoncognito.com
    public_base_url: str = "http://localhost:8050"

    # --- Service wiring ---
    api_base_url: str = "http://localhost:8000"
    session_secret: str = Field(default="", repr=False)
    session_ttl_seconds: int = 8 * 3600

    # --- Queue (local = Postgres-backed adapter, sqs = AWS) ---
    queue_backend: Literal["local", "sqs"] = "local"
    sqs_queue_url: str = ""
    sqs_region: str = "us-east-1"
    queue_visibility_timeout_s: int = 300
    queue_max_receive_count: int = 3  # enforced by the SQS redrive policy / local adapter

    # --- Object storage (local directory or S3) ---
    object_store_backend: Literal["local", "s3"] = "local"
    object_store_path: str = "./.data/objects"
    s3_bucket: str = ""
    aws_region: str = "us-east-1"

    # --- LLM ---
    llm_provider: Literal["openai", "stub", "bedrock"] = "openai"
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "HRM_OPENAI_API_KEY"),
    )
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "none"
    llm_timeout_seconds: float = Field(default=60, gt=0, le=120)
    bedrock_model_id: str = ""  # selected after evaluation; see docs/pilot_scope.md
    bedrock_region: str = "us-east-1"

    @property
    def llm_model(self) -> str | None:
        if self.llm_provider == "openai":
            return self.openai_model
        return self.bedrock_model_id if self.llm_provider == "bedrock" else None

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "openai":
            return bool(
                self.openai_api_key.get_secret_value().strip() and self.openai_model.strip()
            )
        return bool(self.bedrock_model_id) if self.llm_provider == "bedrock" else True

    # --- Operational limits ---
    max_jobs_per_user_active: int = 3
    job_timeout_seconds: int = 600
    job_lease_seconds: int = 300
    solver_max_seconds: float = 30.0
    llm_max_calls_per_job: int = 4
    llm_max_tokens_per_job: int = 12_000
    llm_max_output_tokens: int = 1_024
    llm_max_retries: int = 1
    max_tool_calls_per_job: int = Field(default=24, ge=2, le=100)
    max_analysis_runs_per_job: int = Field(default=10, ge=1, le=10)
    max_comparison_scenarios: int = Field(default=5, ge=1, le=5)
    max_sensitivity_runs: int = Field(default=5, ge=1, le=5)
    solver_total_seconds_per_job: float = Field(default=90.0, gt=0, le=300)
    max_result_bytes: int = Field(default=1_000_000, ge=100_000, le=5_000_000)
    source_freshness_days: int = Field(default=730, ge=1, le=3650)
    inventory_freshness_days: int = Field(default=365, ge=1, le=3650)
    road_freshness_days: int = Field(default=365, ge=1, le=3650)
    max_map_features: int = 3_000
    max_candidate_sites: int = 50
    max_new_centers: int = 3

    @model_validator(mode="after")
    def _guard_non_local(self) -> Settings:
        if self.environment in {"staging", "prod"}:
            problems = []
            if self.auth_mode != "cognito":
                problems.append("HRM_AUTH_MODE must be 'cognito'")
            if self.secret_source != "ssm":  # noqa: S105 - source selector
                problems.append("HRM_SECRET_SOURCE must be 'ssm' in staging/production")
            if len(self.session_secret) < 32:
                problems.append("Provide a session secret of at least 32 characters in SSM")
            if problems:
                raise ValueError("; ".join(problems))
        return self

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        if not self.db_password:
            raise ValueError(
                "Set HRM_DB_PASSWORD in the private local .env, or enable SSM secret loading"
            )
        return (
            f"postgresql+psycopg://{quote(self.db_user)}:{quote(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode={self.db_sslmode}"
        )

    @property
    def psycopg_dsn(self) -> str:
        return self.sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)

    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8050"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
