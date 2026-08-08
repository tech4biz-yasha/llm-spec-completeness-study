"""T13 step 3 — document upload and retrieval."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Path, Response, UploadFile, status

from exit_workflow.api.deps import DocumentServiceDep, SettingsDep, WorkflowServiceDep
from exit_workflow.api.schemas.workflow import DocumentResponse
from exit_workflow.api.v1.common import WORKFLOW_PATH_DESCRIPTION, workflow_identifier
from exit_workflow.core.errors import ValidationError
from exit_workflow.domain.enums import DocumentType

router = APIRouter(tags=["documents"])

WorkflowRef = Annotated[str, Path(description=WORKFLOW_PATH_DESCRIPTION)]


@router.post(
    "/exit-workflows/{workflow_ref}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a document to the exit workflow",
)
async def upload_document(
    workflow_ref: WorkflowRef,
    workflows: WorkflowServiceDep,
    documents: DocumentServiceDep,
    settings: SettingsDep,
    document_type: Annotated[DocumentType, Form()],
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)

    # Read with a hard cap so an oversized upload cannot exhaust memory: one
    # byte past the limit is enough to reject it.
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise ValidationError(
            f"File exceeds the {settings.max_upload_bytes} byte limit.",
            extra={"max_bytes": settings.max_upload_bytes},
        )

    document = await documents.upload(
        workflow,
        document_type=document_type,
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    return DocumentResponse.model_validate(document)


@router.get(
    "/exit-workflows/{workflow_ref}/documents",
    response_model=list[DocumentResponse],
    summary="List documents attached to the exit workflow",
)
async def list_documents(
    workflow_ref: WorkflowRef,
    workflows: WorkflowServiceDep,
    documents: DocumentServiceDep,
    document_type: DocumentType | None = None,
) -> list[DocumentResponse]:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    rows = await documents.list_for_workflow(workflow, document_type=document_type)
    return [DocumentResponse.model_validate(row) for row in rows]


@router.get(
    "/documents/{document_id}/content",
    response_class=Response,
    summary="Download a document",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_document(
    document_id: uuid.UUID, documents: DocumentServiceDep
) -> Response:
    document, data = await documents.download(document_id)
    return Response(
        content=data,
        media_type=document.content_type,
        headers={
            # Always an attachment, never inline: uploaded content is untrusted
            # and must not be rendered in the platform's origin.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(data)),
        },
    )
