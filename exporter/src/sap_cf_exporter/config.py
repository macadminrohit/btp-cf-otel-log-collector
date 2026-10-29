"""Configuration, loaded from environment variables and an optional ``.env`` file.

Secrets are ``SecretStr`` so they are masked in ``repr()``, logs and validation errors.
The ``.env`` file must stay out of Git (see ``.gitignore``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _split_csv(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def validate_cf_url(url: str, *, name: str) -> str:
    """Require HTTPS (plain HTTP only for loopback, e.g. a local mock)."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"{name} must be an absolute URL")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    ):
        raise ValueError(f"{name} must use https")
    return url.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `docker compose` and blank lines in .env yield "" for unset values.
        env_ignore_empty=True,
    )

    # --- Runtime -----------------------------------------------------------------
    exporter_source: Literal["live", "fixture"] = Field(
        "live",
        description="'live' polls the CF API; 'fixture' serves a saved /stats response "
        "(Milestone 1, needs no credentials).",
    )
    exporter_host: str = "127.0.0.1"  # the Docker image overrides this to 0.0.0.0
    exporter_port: int = Field(8000, ge=1, le=65535)
    log_level: str = "INFO"

    # --- Cloud Foundry API / auth ------------------------------------------------
    cf_api_url: str = "https://api.cf.us10.hana.ondemand.com"
    cf_uaa_url: str | None = Field(
        None, description="Override UAA URL; discovered from the CF API root when unset."
    )
    cf_auth_mode: Literal["client_credentials", "password", "static_token"] = "client_credentials"
    cf_client_id: str | None = None
    cf_client_secret: SecretStr | None = None
    cf_username: str | None = None
    cf_password: SecretStr | None = None
    cf_bearer_token: SecretStr | None = None

    # --- Where the container numbers come from ------------------------------------
    metrics_backend: Literal["stats_api", "log_cache"] = Field(
        "stats_api",
        description="'stats_api' reads /v3/processes/{guid}/stats; 'log_cache' reads the "
        "Loggregator gauges kept in Log Cache. Both need only an ordinary user login.",
    )
    log_cache_url: str | None = Field(
        None,
        description="Log Cache base URL. Default: the CF API host with its 'api.' prefix "
        "replaced by 'log-cache.'.",
    )
    log_cache_max_age_seconds: float = Field(
        180.0,
        gt=0,
        description="Ignore Log Cache gauges older than this (an instance that stopped "
        "reporting is then not published).",
    )

    # --- Optional push to an OpenTelemetry Collector (OTLP/HTTP JSON) -------------
    otlp_push_endpoint: str | None = Field(
        None,
        description="Full URL of a Collector's OTLP/HTTP metrics path, e.g. "
        "https://<collector-route>/v1/metrics. Unset = no push (Prometheus /metrics only).",
    )
    otlp_push_token: SecretStr | None = Field(
        None, description="Bearer token the Collector's OTLP receiver requires."
    )
    otlp_push_interval_seconds: float | None = Field(
        None, gt=0, description="Default: the poll interval."
    )
    deployment_environment: str = Field(
        "poc", description="Sent as the deployment.environment.name resource attribute."
    )

    # --- Discovery scope ---------------------------------------------------------
    cf_org_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cf_space_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cf_app_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cf_app_guids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Replica coordination (static sharding by app GUID) ----------------------
    shard_count: int = Field(1, ge=1)
    shard_index: int = Field(0, ge=0)

    # --- Polling -----------------------------------------------------------------
    poll_interval_seconds: float = Field(60.0, gt=0)
    discovery_interval_seconds: float = Field(300.0, gt=0)
    stale_after_seconds: float | None = Field(None, gt=0, description="Default: 3 x poll interval.")
    http_timeout_seconds: float = Field(15.0, gt=0)
    max_concurrency: int = Field(5, ge=1, le=64)
    max_retries: int = Field(4, ge=0, le=10)
    retry_backoff_base_seconds: float = Field(1.0, ge=0)
    retry_backoff_max_seconds: float = Field(30.0, ge=0)
    retry_after_max_seconds: float = Field(120.0, ge=0)

    # --- Fixture mode (Milestone 1) ----------------------------------------------
    fixture_stats_file: Path = Path("fixtures/sample-web-stats.json")
    fixture_org_name: str = "fixture-org"
    fixture_space_name: str = "fixture-space"
    fixture_app_name: str = "sample-app"
    fixture_app_guid: str = "11111111-2222-4333-8444-555555555555"
    fixture_process_guid: str = "11111111-2222-4333-8444-555555555555"
    fixture_process_type: str = "web"

    @field_validator(
        "cf_org_names", "cf_space_names", "cf_app_names", "cf_app_guids", mode="before"
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("cf_api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        return validate_cf_url(value, name="CF_API_URL")

    @field_validator("cf_uaa_url")
    @classmethod
    def _check_uaa_url(cls, value: str | None) -> str | None:
        return validate_cf_url(value, name="CF_UAA_URL") if value else None

    @field_validator("log_cache_url")
    @classmethod
    def _check_log_cache_url(cls, value: str | None) -> str | None:
        return validate_cf_url(value, name="LOG_CACHE_URL") if value else None

    @field_validator("otlp_push_endpoint")
    @classmethod
    def _check_push_endpoint(cls, value: str | None) -> str | None:
        return validate_cf_url(value, name="OTLP_PUSH_ENDPOINT") if value else None

    @property
    def effective_log_cache_url(self) -> str:
        if self.log_cache_url:
            return self.log_cache_url
        host = urlparse(self.cf_api_url)
        name = host.hostname or ""
        if not name.startswith("api."):
            raise ValueError("Set LOG_CACHE_URL: it cannot be derived from CF_API_URL")
        netloc = "log-cache." + name[len("api.") :] + (f":{host.port}" if host.port else "")
        return f"{host.scheme}://{netloc}"

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.otlp_push_endpoint:
            host = urlparse(self.otlp_push_endpoint).hostname
            if host not in LOOPBACK_HOSTS and not self.otlp_push_token:
                raise ValueError("OTLP_PUSH_TOKEN is required when OTLP_PUSH_ENDPOINT is set")
        if self.exporter_source == "live" and self.metrics_backend == "log_cache":
            self.effective_log_cache_url  # noqa: B018 - raises if it cannot be derived
        if self.shard_index >= self.shard_count:
            raise ValueError("SHARD_INDEX must be lower than SHARD_COUNT")
        if self.exporter_source == "live":
            self._check_auth()
        return self

    def _check_auth(self) -> None:
        mode = self.cf_auth_mode
        missing: list[str] = []
        if mode == "client_credentials":
            if not self.cf_client_id:
                missing.append("CF_CLIENT_ID")
            if not self.cf_client_secret:
                missing.append("CF_CLIENT_SECRET")
        elif mode == "password":
            if not self.cf_username:
                missing.append("CF_USERNAME")
            if not self.cf_password:
                missing.append("CF_PASSWORD")
        elif mode == "static_token" and not self.cf_bearer_token:
            missing.append("CF_BEARER_TOKEN")
        if missing:
            raise ValueError(
                f"CF_AUTH_MODE={mode} requires: {', '.join(missing)} "
                "(or set EXPORTER_SOURCE=fixture to run without credentials)"
            )

    @property
    def effective_stale_after_seconds(self) -> float:
        return self.stale_after_seconds or self.poll_interval_seconds * 3
