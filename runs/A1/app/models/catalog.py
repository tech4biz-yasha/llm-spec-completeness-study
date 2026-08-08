"""Parties, properties and contracts.

In the wider Meridian platform these records are owned by the Property service and mastered in
MongoDB. This module keeps a relational projection of the few fields the exit workflow needs
so that PostgreSQL can enforce referential integrity on settlements and audit rows (SRS §15
lists both stores for T13). Writes here come from the platform's replication path; the exit
workflow itself only reads them, except for ``Contract.status``.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, MoneyColumn, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:
    from app.models.workflow import ExitWorkflow


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class Owner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "owners"

    full_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(sa.String(32))

    properties: Mapped[list[Property]] = relationship(back_populates="owner")


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    full_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(sa.String(32))
    emirates_id: Mapped[str | None] = mapped_column(sa.String(32))


class Property(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "properties"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("owners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    address_line: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    community: Mapped[str | None] = mapped_column(sa.String(120))
    city: Mapped[str] = mapped_column(sa.String(80), nullable=False, default="Dubai")
    emirate: Mapped[str] = mapped_column(sa.String(80), nullable=False, default="Dubai")

    owner: Mapped[Owner] = relationship(back_populates="properties")

    @property
    def full_address(self) -> str:
        parts = [self.address_line, self.community, self.city, self.emirate]
        return ", ".join(p for p in parts if p)


class Contract(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (
        sa.CheckConstraint("security_deposit_fils >= 0", name="ck_contracts_deposit_non_negative"),
        sa.CheckConstraint("annual_rent_fils >= 0", name="ck_contracts_rent_non_negative"),
        sa.CheckConstraint("end_date > start_date", name="ck_contracts_date_order"),
        sa.Index("ix_contracts_property_status", "property_id", "status"),
        sa.Index("ix_contracts_tenant_status", "tenant_id", "status"),
    )

    contract_number: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    property_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("owners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[ContractStatus] = mapped_column(
        pg_enum(ContractStatus, "contract_status"),
        nullable=False,
        default=ContractStatus.ACTIVE,
    )
    start_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    end_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    #: The security deposit held against this contract, in fils.
    security_deposit_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)
    annual_rent_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)

    property: Mapped[Property] = relationship(lazy="joined")
    tenant: Mapped[Tenant] = relationship(lazy="joined")
    owner: Mapped[Owner] = relationship(lazy="joined")
    exit_workflows: Mapped[list[ExitWorkflow]] = relationship(back_populates="contract")


class InspectionAgency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered third-party inspection agency (SRS O15)."""

    __tablename__ = "inspection_agencies"

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(sa.String(32))
    trade_license_number: Mapped[str | None] = mapped_column(sa.String(64))
    #: SHA-256 of the agency's API key. The key itself is never stored.
    api_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
