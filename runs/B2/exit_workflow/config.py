"""Module configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: rules.yaml#EXIT-09 — "stored in the UAE region bucket". These are the AWS
#: regions physically in the UAE; a bucket outside them fails configuration.
UAE_REGIONS: Final[frozenset[str]] = frozenset({"me-central-1"})

#: rules.yaml#EXIT-04 — "exponential backoff, 5 attempts, then dead-letter".
NOTIFICATION_MAX_ATTEMPTS: Final[int] = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXIT_WORKFLOW_", extra="ignore")

    database_url: str = "postgresql+asyncpg://localhost/meridian"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 15_000

    # rules.yaml#EXIT-04 — owner notification event.
    kafka_topic_owner_notification: str = "meridian.exit-workflow.owner-notification.v1"
    notification_max_attempts: int = NOTIFICATION_MAX_ATTEMPTS
    notification_backoff_base_seconds: float = 2.0
    notification_backoff_cap_seconds: float = 600.0

    # rules.yaml#EXIT-09 — NOC storage.
    noc_bucket: str = "meridian-noc-uae"
    noc_region: str = "me-central-1"
    noc_key_prefix: str = "exit-workflows"

    @field_validator("noc_region")
    @classmethod
    def _uae_region_only(cls, value: str) -> str:
        # rules.yaml#EXIT-09 — UAE region bucket, checked at startup not at issue time.
        if value not in UAE_REGIONS:
            raise ValueError(
                f"noc_region {value!r} is not a UAE region; rules.yaml#EXIT-09 requires "
                f"one of {sorted(UAE_REGIONS)}"
            )
        return value

    @field_validator("notification_max_attempts")
    @classmethod
    def _five_attempts(cls, value: int) -> int:
        # rules.yaml#EXIT-04 fixes the attempt count at 5.
        if value != NOTIFICATION_MAX_ATTEMPTS:
            raise ValueError("rules.yaml#EXIT-04 fixes owner-notification attempts at 5")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
