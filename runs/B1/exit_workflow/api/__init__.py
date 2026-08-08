"""HTTP surface. Endpoints, request/response shapes and codes come from api.yaml."""

from exit_workflow.api.routes import router

__all__ = ["router"]
