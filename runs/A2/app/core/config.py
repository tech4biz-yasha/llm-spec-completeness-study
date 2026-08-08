"""Application configuration.

Every operational knob that the SRS leaves unspecified is surfaced here rather than
hard-coded, so deployments can align the module with the tenancy law / commercial
policy in force without a code change.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EXITFLOW_",
        extra="ignore",
        frozen=True,
    )

    # ---------------------------------------------------------------- app
    app_name: str = "meridian-exit-workflow"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    root_path: str = ""
    api_prefix: str = "/api/v1"
    cors_allow_origins: list[str] = Field(default_factory=list)

    # ---------------------------------------------------------- database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/meridian"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout_seconds: float = 5.0
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 5_000
    db_echo: bool = False

    # ---------------------------------------------------------------- auth
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    jwt_secret: str = "change-me-in-every-non-local-environment"
    jwt_public_key: str | None = None
    jwt_issuer: str = "meridian-identity"
    jwt_audience: str = "meridian-api"
    jwt_leeway_seconds: int = 30

    # Shared secret used to authenticate the payment provider's webhook callbacks.
    payment_webhook_secret: str = "change-me-payment-webhook-secret"
    # Tolerated clock skew for webhook signature timestamps.
    payment_webhook_tolerance_seconds: int = 300

    # ------------------------------------------------- business policies
    currency: str = "AED"

    #: Minimum notice, in days, between exit submission and the requested move-out date.
    #: SRS is silent; 30 days reflects the customary Dubai notice period for the MVP.
    min_notice_days: int = 30
    #: Furthest future move-out date accepted at submission.
    max_move_out_horizon_days: int = 365
    #: Minimum number of supporting documents required before an exit can be submitted
    #: (SRS T13 step 4 mandates "document upload" without naming a set).
    min_documents_for_submission: int = 1
    #: Document types that must be present before submission, if the operator wants a set.
    required_document_types: list[str] = Field(default_factory=list)
    #: Window during which the tenant may dispute the damage assessment.
    dispute_window_days: int = 5
    #: Days after NOC issuance before the workflow auto-completes if nobody closes it.
    auto_complete_after_noc_days: int = 7
    #: Owner decision SLA on a submitted exit request (used for reminders / reporting).
    owner_approval_sla_days: int = 3
    #: Grace period after which an abandoned DRAFT is expired by the reconciler.
    draft_expiry_days: int = 30

    # --------------------------------------------------------- documents
    max_document_bytes: int = 15 * 1024 * 1024
    allowed_document_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/heic",
            "image/webp",
        ]
    )
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./var/exit-documents"
    storage_s3_bucket: str | None = None
    storage_s3_region: str | None = None
    storage_s3_endpoint_url: str | None = None

    # ------------------------------------------------------------ events
    event_publisher: Literal["outbox-only", "kafka", "log"] = "outbox-only"
    kafka_bootstrap_servers: str | None = None
    kafka_topic: str = "property.exit-workflow.v1"
    outbox_batch_size: int = 100
    outbox_poll_interval_seconds: float = 1.0
    outbox_max_attempts: int = 12
    enable_background_workers: bool = True

    # ------------------------------------------------------------ audit
    #: SRS A3 mandates 7-year retention on audit trails.
    audit_retention_years: int = 7

    # -------------------------------------------------------------- NOC
    noc_issuer_name: str = "Meridian Property Management"
    noc_issuer_address: str = "Dubai, United Arab Emirates"
    noc_verification_base_url: str = "https://verify.meridian.example/noc"
    #: Requests per minute allowed against the unauthenticated NOC verification endpoint.
    noc_verification_rate_limit_per_minute: int = 30

    # ---------------------------------------------------- notifications
    notification_backend: Literal["log", "smtp", "http"] = "log"
    notification_webhook_url: str | None = None
    support_email: str = "support@meridian.example"

    # ---------------------------------------------------------- payments
    payment_backend: Literal["null", "http"] = "null"
    payment_api_base_url: str | None = None
    payment_api_key: str | None = None
    payment_request_timeout_seconds: float = 10.0

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        return v.upper()

    @field_validator("required_document_types")
    @classmethod
    def _upper_doc_types(cls, v: list[str]) -> list[str]:
        return [item.strip().upper() for item in v if item.strip()]

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        if self.environment == "production":
            if self.jwt_algorithm == "HS256" and self.jwt_secret.startswith("change-me"):
                raise ValueError("EXITFLOW_JWT_SECRET must be set in production")
            if self.jwt_algorithm == "RS256" and not self.jwt_public_key:
                raise ValueError("EXITFLOW_JWT_PUBLIC_KEY must be set when using RS256")
            if self.payment_webhook_secret.startswith("change-me"):
                raise ValueError("EXITFLOW_PAYMENT_WEBHOOK_SECRET must be set in production")
            if self.storage_backend == "s3" and not self.storage_s3_bucket:
                raise ValueError("EXITFLOW_STORAGE_S3_BUCKET must be set when storage is s3")
        return self

    @property
    def jwt_verification_key(self) -> str:
        if self.jwt_algorithm == "RS256":
            if not self.jwt_public_key:
                raise RuntimeError("RS256 configured without a public key")
            return self.jwt_public_key
        return self.jwt_secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
