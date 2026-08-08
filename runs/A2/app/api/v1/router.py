"""v1 API router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    contract_guard,
    documents,
    exit_workflows,
    inspections,
    noc,
    settlement,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(exit_workflows.router)
api_router.include_router(documents.router)
api_router.include_router(inspections.router)
api_router.include_router(settlement.router)
api_router.include_router(noc.router)
api_router.include_router(contract_guard.router)
api_router.include_router(webhooks.router)
