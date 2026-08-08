"""Document upload / download (SRS T13 step 4, O16 photos)."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from app.domain import events as ev
from app.domain.enums import (
    AGENCY_DOCUMENT_TYPES,
    SYSTEM_DOCUMENT_TYPES,
    ActorRole,
    DocumentType,
    ExitWorkflowState,
    ScanStatus,
)
from app.domain.events import DomainEvent
from app.models.document import ExitDocument
from app.models.exit_workflow import ExitWorkflow
from app.ports.storage import DocumentNotStoredError, DocumentStorage
from app.repositories.support import DocumentRepository
from app.services.workflow_engine import WorkflowEngine

#: States in which a party may still attach evidence.
_UPLOADABLE_STATES = frozenset(
    {
        ExitWorkflowState.DRAFT,
        ExitWorkflowState.SUBMITTED,
        ExitWorkflowState.OWNER_APPROVED,
        ExitWorkflowState.INSPECTION_SLOTS_PROPOSED,
        ExitWorkflowState.INSPECTION_SCHEDULED,
        ExitWorkflowState.INSPECTION_COMPLETED,
        ExitWorkflowState.DAMAGE_REVIEW,
    }
)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAGIC_PREFIXES: tuple[tuple[bytes, tuple[str, ...]], ...] = (
    (b"%PDF-", ("application/pdf",)),
    (b"\xff\xd8\xff", ("image/jpeg",)),
    (b"\x89PNG\r\n\x1a\n", ("image/png",)),
    (b"RIFF", ("image/webp",)),
)


def sanitise_filename(raw: str) -> str:
    """Reduce a client-supplied filename to something safe to echo and store."""
    name = PurePosixPath(raw.replace("\\", "/")).name.strip() or "upload"
    cleaned = _SAFE_FILENAME.sub("_", name).lstrip(".")
    return (cleaned or "upload")[:180]


class DocumentService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        engine: WorkflowEngine,
        storage: DocumentStorage,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._engine = engine
        self._storage = storage
        self._repo = DocumentRepository(session)

    # ---------------------------------------------------------- commands
    async def upload(
        self,
        workflow: ExitWorkflow,
        *,
        document_type: DocumentType,
        filename: str,
        content_type: str,
        data: bytes,
        caption: str | None,
        ctx: RequestContext,
    ) -> ExitDocument:
        self._authorise_upload(workflow, document_type, ctx)
        self._validate_payload(content_type, data)

        if workflow.state not in _UPLOADABLE_STATES:
            raise ConflictError(
                "Documents can no longer be added to this exit workflow.",
                code="uploads_closed",
                details={"state": workflow.state.value},
            )

        safe_name = sanitise_filename(filename)
        document_id = uuid.uuid4()
        storage_key = self._storage_key(workflow, document_id, document_type, safe_name)

        stored = await self._storage.put(
            key=storage_key,
            data=data,
            content_type=content_type,
            metadata={
                "workflow_id": str(workflow.id),
                "document_type": document_type.value,
                "uploaded_by": str(ctx.principal.actor_id),
            },
        )

        document = ExitDocument(
            id=document_id,
            workflow_id=workflow.id,
            document_type=document_type,
            filename=safe_name,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.storage_key,
            checksum_sha256=stored.checksum_sha256,
            uploaded_by=ctx.principal.actor_id,
            uploaded_by_role=ctx.principal.role,
            # No scanner is wired in this module; the platform's scanning pipeline flips
            # this to CLEAN/INFECTED out of band. SKIPPED keeps the document usable while
            # recording honestly that it was not scanned here.
            scan_status=ScanStatus.SKIPPED,
            caption=caption,
        )
        self._repo.add(document)

        self._engine.audit(
            ctx,
            action="upload_document",
            entity_type="document",
            entity_id=document.id,
            workflow=workflow,
            changes={
                "document_type": document_type.value,
                "filename": safe_name,
                "size_bytes": stored.size_bytes,
                "checksum_sha256": stored.checksum_sha256,
            },
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.DOCUMENT_UPLOADED,
                workflow_id=workflow.id,
                payload={
                    "document_id": str(document.id),
                    "document_type": document_type.value,
                    "size_bytes": stored.size_bytes,
                },
            ),
            ctx,
        )
        return document

    async def remove(
        self,
        workflow: ExitWorkflow,
        document_id: uuid.UUID,
        *,
        reason: str | None,
        ctx: RequestContext,
    ) -> ExitDocument:
        """Soft-delete a document.

        Only the uploader (or an admin) may withdraw a document, and only while the
        workflow is still a draft -- once submitted, the file is evidence.
        """
        document = await self._repo.require_in_workflow(document_id, workflow.id)
        if document.deleted_at is not None:
            return document

        if ctx.principal.role is not ActorRole.ADMIN:
            if document.uploaded_by != ctx.principal.actor_id:
                raise AuthorizationError("Only the uploader may remove this document.")
            if workflow.state is not ExitWorkflowState.DRAFT:
                raise ConflictError(
                    "Documents cannot be removed after the exit request has been submitted.",
                    code="document_immutable",
                    details={"state": workflow.state.value},
                )

        document.deleted_at = self._clock.now()
        document.deleted_by = ctx.principal.actor_id

        self._engine.audit(
            ctx,
            action="remove_document",
            entity_type="document",
            entity_id=document.id,
            workflow=workflow,
            changes={"deleted": True},
            context={"reason": reason} if reason else None,
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.DOCUMENT_REMOVED,
                workflow_id=workflow.id,
                payload={"document_id": str(document.id), "reason": reason},
            ),
            ctx,
        )
        return document

    # ------------------------------------------------------------ reads
    async def list_for(self, workflow: ExitWorkflow) -> list[ExitDocument]:
        return await self._repo.list_active(workflow.id)

    async def fetch(
        self, workflow: ExitWorkflow, document_id: uuid.UUID, ctx: RequestContext
    ) -> tuple[ExitDocument, bytes]:
        document = await self._repo.require_in_workflow(document_id, workflow.id)
        if document.deleted_at is not None:
            raise NotFoundError("This document has been removed.")
        if not document.is_downloadable:
            raise ConflictError(
                "This document is not available for download.",
                code="document_unavailable",
                details={"scan_status": document.scan_status.value},
            )
        try:
            data = await self._storage.get(document.storage_key)
        except DocumentNotStoredError as exc:
            raise NotFoundError("The stored file could not be located.") from exc

        # A mismatch means the blob store and the metadata have diverged; serving the
        # bytes anyway would undermine every checksum we have published.
        if hashlib.sha256(data).hexdigest() != document.checksum_sha256:
            raise ConflictError(
                "The stored file failed its integrity check and was not served.",
                code="document_integrity_failure",
                details={"document_id": str(document.id)},
            )

        self._engine.audit(
            ctx,
            action="download_document",
            entity_type="document",
            entity_id=document.id,
            workflow=workflow,
        )
        return document, data

    # ---------------------------------------------------------- helpers
    def _authorise_upload(
        self, workflow: ExitWorkflow, document_type: DocumentType, ctx: RequestContext
    ) -> None:
        role = ctx.principal.role
        if document_type in SYSTEM_DOCUMENT_TYPES:
            raise ValidationFailedError(
                f"{document_type.value} documents are generated by the system and "
                "cannot be uploaded.",
                details={"field": "document_type"},
            )

        agency_id = workflow.inspection.agency_id if workflow.inspection else None
        self._engine.authorise_party(workflow, ctx, agency_id=agency_id, action="upload")

        if document_type in AGENCY_DOCUMENT_TYPES and role not in (
            ActorRole.INSPECTION_AGENCY,
            ActorRole.ADMIN,
        ):
            raise AuthorizationError(
                f"Only the inspection agency may upload {document_type.value} documents."
            )
        if document_type not in AGENCY_DOCUMENT_TYPES and role is ActorRole.INSPECTION_AGENCY:
            raise AuthorizationError(
                "Inspection agencies may only upload inspection reports and damage photos."
            )

    def _validate_payload(self, content_type: str, data: bytes) -> None:
        if not data:
            raise ValidationFailedError("The uploaded file is empty.")
        if len(data) > self._settings.max_document_bytes:
            raise PayloadTooLargeError(
                "The uploaded file exceeds the maximum allowed size.",
                details={
                    "max_bytes": self._settings.max_document_bytes,
                    "size_bytes": len(data),
                },
            )
        normalised = content_type.split(";")[0].strip().lower()
        if normalised not in self._settings.allowed_document_content_types:
            raise UnsupportedMediaTypeError(
                f"{normalised or 'unknown'} files are not accepted.",
                details={"allowed": self._settings.allowed_document_content_types},
            )
        # Trusting the declared Content-Type alone lets a caller store a script under an
        # image/png label; check the magic bytes agree with what they claim.
        for prefix, types in _MAGIC_PREFIXES:
            if data.startswith(prefix):
                if normalised in types or (
                    prefix == b"RIFF" and data[8:12] == b"WEBP" and normalised == "image/webp"
                ):
                    return
                raise UnsupportedMediaTypeError(
                    "The file contents do not match the declared content type.",
                    details={"declared": normalised},
                )
        if normalised in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
            raise UnsupportedMediaTypeError(
                "The file contents do not match the declared content type.",
                details={"declared": normalised},
            )
        # image/heic and any future additions have no stable magic prefix we check for.

    @staticmethod
    def _storage_key(
        workflow: ExitWorkflow,
        document_id: uuid.UUID,
        document_type: DocumentType,
        filename: str,
    ) -> str:
        return (
            f"exit-workflows/{workflow.id}/{document_type.value.lower()}/"
            f"{document_id}/{filename}"
        )
