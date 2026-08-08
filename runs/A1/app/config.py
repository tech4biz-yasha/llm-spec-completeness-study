"""Application configuration.

All tunables are environment-driven so the same image runs in every environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ---------------------------------------------------------------
    debug: bool = False
    environment: Literal["local", "test", "staging", "production"] = "local"
    api_prefix: str = "/api/v1"

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/exit_workflow"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 10
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 5_000
    db_echo: bool = False

    # --- Security -----------------------------------------------------------
    # HS256 secret used to verify access tokens minted by the platform identity service.
    jwt_secret: str = "change-me-in-production"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_leeway_seconds: int = 30

    # --- Messaging ----------------------------------------------------------
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_prefix: str = "meridian.exit"

    # --- Notifications ------------------------------------------------------
    notifications_enabled: bool = True

    # --- Outbox relay -------------------------------------------------------
    outbox_relay_enabled: bool = True
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 100

    # --- Domain rules -------------------------------------------------------
    # Minimum notice, in days, between the exit request and the requested move-out date.
    # The SRS does not fix a value; it is configurable per market.
    min_notice_days: int = 0
    # Furthest into the future a move-out date may be set.
    max_notice_days: int = 365
    # Required document kinds before a workflow may be submitted to the owner (T13 step 4).
    required_document_kinds: tuple[str, ...] = ("EMIRATES_ID",)
    # Audit retention, per SRS A3.
    audit_retention_years: int = 7
    currency: str = "AED"
    # Dubai-only for MVP; move-out dates are validated against the local calendar day,
    # not UTC, so a request at 02:00 Gulf time is not judged against yesterday.
    market_timezone: str = "Asia/Dubai"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// driver")
        return v

    @field_validator("required_document_kinds", mode="before")
    @classmethod
    def _split_docs(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(part.strip() for part in v.split(",") if part.strip())
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.jwt_secret == "change-me-in-production":
        raise RuntimeError("JWT_SECRET must be set to a real secret in production")
    return settings
