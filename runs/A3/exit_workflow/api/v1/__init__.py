"""API v1 router assembly."""

from fastapi import APIRouter

from exit_workflow.api.v1 import (
    documents,
    eligibility,
    exit_workflows,
    inspections,
    meta,
    noc,
    settlements,
)

api_router = APIRouter()
api_router.include_router(exit_workflows.router)
api_router.include_router(documents.router)
api_router.include_router(inspections.router)
api_router.include_router(settlements.router)
api_router.include_router(noc.router)
api_router.include_router(eligibility.router)
api_router.include_router(meta.router)

__all__ = ["api_router"]
