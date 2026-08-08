"""Deployment configuration.

Values that the kit fixes (retry count, stall window, currency) are constants in
the code with their rule ID, not settings — an operator must not be able to
change a decided rule from an environment variable. Settings here cover
infrastructure and the one piece of reference data the kit has not published
(``exit_reason_codes``, blockers.md#B-1).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Decided by the kit; not configurable. -----------------------------------

#: rules.yaml#EXIT-04 — 5 dispatch attempts, then dead-letter + admin alert.
NOTIFICATION_MAX_ATTEMPTS: Final[int] = 5

#: rules.yaml#EXIT-05 — inspection must be scheduled within 30 days of move-out.
STALL_THRESHOLD_DAYS: Final[int] = 30

#: rules.yaml#EXIT-10 — audit retention.
AUDIT_RETENTION_YEARS: Final[int] = 7


class Settings(BaseSettings):
    """Environment-driven settings, prefix ``EXIT_``."""

    model_config = SettingsConfigDict(env_prefix="EXIT_", extra="ignore")

    environment: str = "production"

    # PostgreSQL — payments, audit, workflow document, exit lock (AGENTS.md).
    database_url: str = "postgresql+asyncpg://localhost/exit_workflow"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # blockers.md#B-1: the exit reason reference list is not published. Empty
    # here on purpose; populating it with invented values is forbidden.
    exit_reason_codes: list[str] | None = None

    # Kafka — owner notification and workflow events (AGENTS.md).
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_events_topic: str = "proptech.exit-workflow.events"
    kafka_dead_letter_topic: str = "proptech.exit-workflow.events.dlq"
    kafka_enabled: bool = True

    #: Exponential backoff between notification attempts (rules.yaml#EXIT-04).
    notification_backoff_base_seconds: float = 2.0
    notification_backoff_factor: float = 2.0
    notification_backoff_max_seconds: float = 900.0

    # NOC storage — rules.yaml#EXIT-09: UAE region bucket, immutable.
    noc_bucket: str = "meridian-noc-uae"
    noc_region: str = Field(
        default="me-central-1",
        description="Must be a UAE region; enforced by exit_workflow.storage.noc.",
    )
    noc_storage_backend: str = Field(default="s3", pattern="^(s3|filesystem)$")
    noc_filesystem_root: str = "/var/lib/exit-workflow/noc"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
