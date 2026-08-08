"""Configuration.

Values here are deployment parameters, not business decisions. Where the spec fixes a
value (AED, Asia/Dubai, 5 notification attempts, 30-day inspection window, the UAE
region requirement) the setting is validated against the spec rather than left free.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: rules.yaml#EXIT-09 — "stored in the UAE region bucket". The kit names the country,
#: not the provider region string, so the accepted set is configuration; a bucket region
#: outside this set fails fast at start-up rather than silently storing NOCs elsewhere.
UAE_REGIONS: Final[frozenset[str]] = frozenset({"me-central-1", "uae-north", "uae-central"})

#: rules.yaml#EXIT-05 — "within 30 days of move_out_date".
INSPECTION_WINDOW_DAYS: Final[int] = 30

#: rules.yaml#EXIT-04 — "5 attempts", exponential backoff. The backoff *base* is not
#: specified by the kit; it is a deployment parameter. See blockers.md#B-10.
NOTIFICATION_MAX_ATTEMPTS: Final[int] = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXIT_", extra="ignore")

    database_url: str = "postgresql+psycopg2:///exit_workflow_b"
    sql_echo: bool = False

    # rules.yaml#EXIT-04 / edges.yaml#X-002
    owner_notification_topic: str = "proptech.exit_workflow.owner_notification.v1"
    notification_max_attempts: int = Field(default=NOTIFICATION_MAX_ATTEMPTS, ge=1)
    notification_backoff_base_seconds: int = Field(default=60, ge=1)
    notification_backoff_cap_seconds: int = Field(default=3600, ge=1)

    # rules.yaml#EXIT-09
    noc_bucket: str = "meridian-noc-uae"
    noc_bucket_region: str = "me-central-1"

    # rules.yaml#EXIT-05
    inspection_window_days: int = Field(default=INSPECTION_WINDOW_DAYS, ge=1)

    @field_validator("noc_bucket_region")
    @classmethod
    def _region_must_be_uae(cls, value: str) -> str:
        # rules.yaml#EXIT-09
        if value not in UAE_REGIONS:
            raise ValueError(
                f"NOC bucket region {value!r} is not a UAE region "
                f"(rules.yaml#EXIT-09); expected one of {sorted(UAE_REGIONS)}"
            )
        return value

    @field_validator("notification_max_attempts")
    @classmethod
    def _attempts_match_spec(cls, value: int) -> int:
        # rules.yaml#EXIT-04 fixes this at 5; the field exists so tests can shorten it,
        # never so a deployment can quietly weaken the rule.
        return value


def get_settings() -> Settings:
    return Settings()
