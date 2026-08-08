"""Contract directory port — the authority on parties and deposit amount.

The exit module must never take the security deposit, the property or the
owner from a client request: a tenant could otherwise inflate their own
refund. Everything about the contract is read from the Property service at
initiation and snapshotted onto the workflow row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from exit_workflow.core.errors import NotFoundError, UpstreamServiceError, ValidationError
from exit_workflow.core.money import quantize

SERVICE_NAME = "property"


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    contract_id: uuid.UUID
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    security_deposit_amount: Decimal
    currency: str = "AED"
    status: str = "ACTIVE"
    end_date: date | None = None
    property_reference: str | None = None
    property_address: str | None = None
    tenant_name: str | None = None
    tenant_email: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None

    def ensure_exitable(self) -> None:
        if self.status.upper() not in ("ACTIVE", "EXPIRING", "EXPIRED"):
            raise ValidationError(
                f"Contract is {self.status}; an exit can only be initiated on an active "
                "contract.",
                extra={"contract_status": self.status},
            )


class ContractDirectory(Protocol):
    async def get_contract(self, contract_id: uuid.UUID) -> ContractSnapshot: ...


class StaticContractDirectory:
    """In-process directory for local development and tests."""

    def __init__(self, contracts: dict[uuid.UUID, ContractSnapshot] | None = None) -> None:
        self._contracts: dict[uuid.UUID, ContractSnapshot] = dict(contracts or {})

    def add(self, snapshot: ContractSnapshot) -> ContractSnapshot:
        self._contracts[snapshot.contract_id] = snapshot
        return snapshot

    async def get_contract(self, contract_id: uuid.UUID) -> ContractSnapshot:
        try:
            return self._contracts[contract_id]
        except KeyError as exc:
            raise NotFoundError(f"Contract {contract_id} was not found.") from exc


class HttpContractDirectory:
    """Adapter for the Property service's contract endpoint."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def get_contract(self, contract_id: uuid.UUID) -> ContractSnapshot:
        import httpx  # imported lazily: unused in deployments using the static adapter

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = f"{self._base_url}/internal/contracts/{contract_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(SERVICE_NAME, str(exc)) from exc

        if response.status_code == 404:
            raise NotFoundError(f"Contract {contract_id} was not found.")
        if response.status_code >= 400:
            raise UpstreamServiceError(
                SERVICE_NAME, f"Contract lookup failed with HTTP {response.status_code}."
            )
        return self._parse(response.json())

    @staticmethod
    def _parse(payload: dict[str, Any]) -> ContractSnapshot:
        try:
            return ContractSnapshot(
                contract_id=uuid.UUID(payload["contract_id"]),
                property_id=uuid.UUID(payload["property_id"]),
                tenant_id=uuid.UUID(payload["tenant_id"]),
                owner_id=uuid.UUID(payload["owner_id"]),
                security_deposit_amount=quantize(str(payload["security_deposit_amount"])),
                currency=payload.get("currency", "AED"),
                status=payload.get("status", "ACTIVE"),
                end_date=date.fromisoformat(payload["end_date"]) if payload.get("end_date") else None,
                property_reference=payload.get("property_reference"),
                property_address=payload.get("property_address"),
                tenant_name=payload.get("tenant_name"),
                tenant_email=payload.get("tenant_email"),
                owner_name=payload.get("owner_name"),
                owner_email=payload.get("owner_email"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise UpstreamServiceError(
                SERVICE_NAME, "Contract payload from the Property service was malformed."
            ) from exc
