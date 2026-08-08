"""Aggregate API router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import agency, contracts, workflows

api_router = APIRouter()
api_router.include_router(workflows.router)
api_router.include_router(agency.router)
api_router.include_router(contracts.router)
