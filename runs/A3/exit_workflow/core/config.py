"""Application settings.

Everything that a deployment might need to tune lives here. Business-policy
knobs are deliberately explicit rather than hard-coded constants because the
SRS extract is silent on several of them (notice period, over-deposit damage
handling); see README "Assumptions and spec gaps".
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "development", "staging", "production"]

_INSECURE_DEFAULT_SECRET = "change-me-in-every-non-local-environment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXITWF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- app ---------------------------------------------------------------
    app_name: str = "meridian-exit-workflow"
    environment: Environment = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_allow_origins: list[str] = Field(default_factory=list)

    # --- database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://localhost:5432/exit_workflow"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 10
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 5_000
    db_echo: bool = False

    # --- auth --------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(_INSECURE_DEFAULT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "meridian-identity"
    jwt_audience: str = "meridian-api"
    jwt_leeway_seconds: int = 30

    # --- business policy ---------------------------------------------------
    currency: str = "AED"
    #: Minimum days between exit initiation and the requested move-out date.
    #: The SRS extract specifies no notice period, so the default disables the
    #: check; set per-market (e.g. 30 or 90) without touching code.
    min_notice_days: int = 0
    #: Upper bound on how far ahead a move-out date may be scheduled.
    max_move_out_horizon_days: int = 365
    #: When damage exceeds the deposit, allow the workflow to proceed with a
    #: zero refund and a recorded tenant balance. If false, such reports must
    #: be adjusted down by the owner before settlement.
    allow_deduction_above_deposit: bool = True
    #: Proposed inspection slots must start at least this far in the future.
    inspection_slot_min_lead_hours: int = 12
    #: Document types a tenant must attach before the request may be submitted.
    required_document_types: list[str] = Field(default_factory=list)

    # --- upstream services -------------------------------------------------
    #: Base URLs of the platform services this module depends on. When unset,
    #: in-process static adapters are used (local/test only — startup fails in
    #: staging/production without them).
    property_service_url: str | None = None
    agency_service_url: str | None = None
    payment_service_url: str | None = None
    internal_service_token: SecretStr | None = None
    upstream_timeout_seconds: float = 2.0
    payment_timeout_seconds: float = 10.0

    # --- document storage --------------------------------------------------
    storage_root: str = "./var/storage"
    max_upload_bytes: int = 20 * 1024 * 1024
    allowed_upload_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/heic",
            "image/webp",
        ]
    )

    # --- eventing ----------------------------------------------------------
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_prefix: str = "meridian.exit"
    background_worker_enabled: bool = True
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 100
    outbox_max_attempts: int = 10

    # --- notifications -----------------------------------------------------
    notification_from_address: str = "no-reply@meridian.ae"
    notification_batch_size: int = 50

    # --- compliance --------------------------------------------------------
    audit_retention_years: int = 7

    # --- identifiers -------------------------------------------------------
    workflow_reference_prefix: str = "EXW"
    noc_number_prefix: str = "NOC"

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        return v.upper()

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        return v

    @model_validator(mode="after")
    def _reject_insecure_production(self) -> Settings:
        if self.environment in ("staging", "production"):
            if self.jwt_secret.get_secret_value() == _INSECURE_DEFAULT_SECRET:
                raise ValueError("EXITWF_JWT_SECRET must be set outside local/test")
            if self.debug:
                raise ValueError("EXITWF_DEBUG must be false outside local/test")
            missing = [
                name
                for name, value in (
                    ("EXITWF_PROPERTY_SERVICE_URL", self.property_service_url),
                    ("EXITWF_AGENCY_SERVICE_URL", self.agency_service_url),
                    ("EXITWF_PAYMENT_SERVICE_URL", self.payment_service_url),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "In-process stub adapters cannot be used outside local/test; set "
                    + ", ".join(missing)
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment in ("staging", "production")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
