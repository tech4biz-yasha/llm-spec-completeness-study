"""Registered inspection agency directory (O15).

O15 says inspections are requested from *registered* agencies, so the agency
id on a request is validated against this directory rather than trusted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from exit_workflow.core.errors import NotFoundError, UpstreamServiceError, ValidationError

SERVICE_NAME = "agency-directory"


@dataclass(frozen=True, slots=True)
class AgencySnapshot:
    agency_id: uuid.UUID
    name: str
    email: str
    is_active: bool = True
    phone: str | None = None
    emirates: tuple[str, ...] = ("DUBAI",)

    def ensure_engageable(self) -> None:
        if not self.is_active:
            raise ValidationError(
                f"Inspection agency {self.name} is not currently registered.",
                extra={"agency_id": str(self.agency_id)},
            )


class AgencyDirectory(Protocol):
    async def get_agency(self, agency_id: uuid.UUID) -> AgencySnapshot: ...

    async def list_active(self) -> list[AgencySnapshot]: ...


class StaticAgencyDirectory:
    def __init__(self, agencies: list[AgencySnapshot] | None = None) -> None:
        self._agencies: dict[uuid.UUID, AgencySnapshot] = {
            a.agency_id: a for a in (agencies or [])
        }

    def add(self, agency: AgencySnapshot) -> AgencySnapshot:
        self._agencies[agency.agency_id] = agency
        return agency

    async def get_agency(self, agency_id: uuid.UUID) -> AgencySnapshot:
        try:
            return self._agencies[agency_id]
        except KeyError as exc:
            raise NotFoundError(f"Inspection agency {agency_id} is not registered.") from exc

    async def list_active(self) -> list[AgencySnapshot]:
        return [a for a in self._agencies.values() if a.is_active]


class HttpAgencyDirectory:
    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def _get(self, path: str) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(SERVICE_NAME, str(exc)) from exc
        if response.status_code == 404:
            raise NotFoundError("Inspection agency is not registered.")
        if response.status_code >= 400:
            raise UpstreamServiceError(
                SERVICE_NAME, f"Agency lookup failed with HTTP {response.status_code}."
            )
        return response.json()

    @staticmethod
    def _parse(payload: dict[str, Any]) -> AgencySnapshot:
        try:
            return AgencySnapshot(
                agency_id=uuid.UUID(payload["agency_id"]),
                name=payload["name"],
                email=payload["email"],
                is_active=bool(payload.get("is_active", True)),
                phone=payload.get("phone"),
                emirates=tuple(payload.get("emirates") or ("DUBAI",)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise UpstreamServiceError(SERVICE_NAME, "Agency payload was malformed.") from exc

    async def get_agency(self, agency_id: uuid.UUID) -> AgencySnapshot:
        return self._parse(await self._get(f"/internal/inspection-agencies/{agency_id}"))

    async def list_active(self) -> list[AgencySnapshot]:
        payload = await self._get("/internal/inspection-agencies?active=true")
        return [self._parse(item) for item in payload.get("items", [])]
