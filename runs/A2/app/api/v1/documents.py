"""Document endpoints (SRS T13 step 4; O16 damage photos)."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.api.deps import (
    ContextDep,
    LockedWorkflowDep,
    ServicesDep,
    SettingsDep,
    WorkflowDep,
)
from app.api.presenters import document as present_document
from app.core.errors import PayloadTooLargeError
from app.domain.enums import DocumentType
from app.schemas.documents import DocumentListResponse, DocumentResponse

router = APIRouter(prefix="/exit-workflows/{workflow_id}/documents", tags=["documents"])

BASE_PATH = "/api/v1/exit-workflows"


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a supporting document",
    description=(
        "SRS T13 step 4. Multipart upload. The declared content type is checked against "
        "the file's magic bytes, and the stored SHA-256 is returned so the client can "
        "verify what was persisted."
    ),
)
async def upload_document(
    workflow: LockedWorkflowDep,
    services: ServicesDep,
    settings: SettingsDep,
    ctx: ContextDep,
    file: Annotated[UploadFile, File(description="The document to attach")],
    document_type: Annotated[DocumentType, Form()],
    caption: Annotated[str | None, Form(max_length=500)] = None,
) -> DocumentResponse:
    # Read one byte past the limit rather than the whole body: an oversized upload is
    # rejected without ever being fully buffered.
    max_bytes = settings.max_document_bytes
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            "The uploaded file exceeds the maximum allowed size.",
            details={"max_bytes": max_bytes},
        )

    doc = await services.documents.upload(
        workflow,
        document_type=document_type,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        caption=caption,
        ctx=ctx,
    )
    return present_document(doc, base_path=BASE_PATH)


@router.get("", response_model=DocumentListResponse, summary="List attached documents")
async def list_documents(
    workflow: WorkflowDep, services: ServicesDep
) -> DocumentListResponse:
    docs = await services.documents.list_for(workflow)
    items = [present_document(d, base_path=BASE_PATH) for d in docs]
    return DocumentListResponse(items=items, total=len(items))


@router.get(
    "/{document_id}/content",
    summary="Download a document",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_document(
    workflow: WorkflowDep,
    document_id: uuid.UUID,
    services: ServicesDep,
    ctx: ContextDep,
) -> Response:
    doc, data = await services.documents.fetch(workflow, document_id, ctx)
    return Response(
        content=data,
        media_type=doc.content_type,
        headers={
            # RFC 5987 encoding keeps non-ASCII filenames intact.
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(doc.filename)}"
            ),
            "Content-Length": str(len(data)),
            "X-Checksum-SHA256": doc.checksum_sha256,
            "Cache-Control": "private, no-store",
        },
    )


@router.delete(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Remove a document from a draft",
    description=(
        "Soft delete. Permitted only while the exit request is still a draft; after "
        "submission the file is evidence and is retained."
    ),
)
async def delete_document(
    workflow: LockedWorkflowDep,
    document_id: uuid.UUID,
    services: ServicesDep,
    ctx: ContextDep,
    reason: Annotated[str | None, Query(max_length=500)] = None,
) -> DocumentResponse:
    doc = await services.documents.remove(workflow, document_id, reason=reason, ctx=ctx)
    return present_document(doc, base_path=BASE_PATH)
