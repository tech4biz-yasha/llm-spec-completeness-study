"""Database guarantees, PDF output and retry policy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text

from exit_workflow.config import NOTIFICATION_MAX_ATTEMPTS
from exit_workflow.db.base import Base
from exit_workflow.domain.states import State
from exit_workflow.events.dispatcher import backoff_delay_seconds
from exit_workflow.storage.noc import (
    FilesystemNocStorage,
    ImmutableObjectExists,
    NocStorageError,
)
from exit_workflow.storage.pdf import PdfRenderError, render_pdf


# --- Append-only audit (AGENTS.md: enforced by DB trigger) --------------------


async def test_audit_rows_cannot_be_updated_or_deleted(session_factory):
    """rules.yaml#EXIT-10 — the trigger refuses, not the application code."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO exit_workflow_audit "
                    "(workflow_id, actor_type, actor_id, from_state, to_state, metadata) "
                    "VALUES ('EX-20260808-00001', 'tenant', 't', NULL, 'INITIATED', '{}')"
                )
            )

    for statement in (
        "UPDATE exit_workflow_audit SET to_state = 'COMPLETE'",
        "DELETE FROM exit_workflow_audit",
        "TRUNCATE exit_workflow_audit",
    ):
        async with session_factory() as session:
            with pytest.raises(Exception, match="append-only"):
                async with session.begin():
                    await session.execute(text(statement))

    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM exit_workflow_audit")) == 1


async def test_noc_documents_are_immutable(session_factory):
    """rules.yaml#EXIT-09 — immutable once issued."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO noc_documents "
                    "(id, workflow_id, bucket, region, object_key, content_sha256, byte_size) "
                    "VALUES (gen_random_uuid(), 'EX-20260808-00001', 'b', 'me-central-1', "
                    "'k', repeat('a', 64), 10)"
                )
            )

    async with session_factory() as session:
        with pytest.raises(Exception, match="append-only"):
            async with session.begin():
                await session.execute(text("UPDATE noc_documents SET object_key = 'other'"))


async def test_one_workflow_per_contract_is_a_database_constraint(session_factory):
    """rules.yaml#EXIT-01 does not depend on application-level checking."""
    async with session_factory() as session:
        constraint = await session.scalar(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'exit_workflows'::regclass AND contype = 'u' "
                "AND conname = 'uq_exit_workflows_contract'"
            )
        )
    assert constraint == "uq_exit_workflows_contract"


async def test_status_check_constraint_matches_states_yaml(session_factory):
    """The database refuses any status states.yaml does not define."""
    async with session_factory() as session:
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_exit_workflows_status'"
            )
        )
    for state in State:
        assert f"'{state.value}'" in definition


async def test_orm_models_match_the_deployed_schema(engine):
    """models.py mirrors migrations/001_initial.sql; drift fails here."""
    async with engine.connect() as connection:
        deployed = await connection.run_sync(
            lambda sync: {
                name: {column["name"] for column in inspect(sync).get_columns(name)}
                for name in inspect(sync).get_table_names()
            }
        )

    for table in Base.metadata.sorted_tables:
        assert table.name in deployed, f"{table.name} is missing from the database"
        mapped = {column.name for column in table.columns}
        assert mapped == deployed[table.name], f"column drift on {table.name}"


async def test_documents_check_constraint_rejects_empty_list(session_factory, tenancy):
    """rules.yaml#EXIT-02 — at least one document, enforced in the schema too."""
    async with session_factory() as session:
        with pytest.raises(Exception, match="ck_exit_workflows_documents_required"):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO exit_workflows (id, contract_id, property_id, tenant_id, "
                        "owner_id, status, move_out_date, reason, documents, "
                        "security_deposit_minor) VALUES ('EX-20260808-00009', :contract_id, "
                        ":property_id, :tenant_id, :owner_id, 'INITIATED', '2026-09-01', 'R', "
                        "'[]'::jsonb, 0)"
                    ),
                    {
                        "contract_id": tenancy.contract_id,
                        "property_id": tenancy.property_id,
                        "tenant_id": tenancy.tenant_id,
                        "owner_id": tenancy.owner_id,
                    },
                )


# --- Retry policy (rules.yaml#EXIT-04) ----------------------------------------


def test_backoff_is_exponential():
    delays = [
        backoff_delay_seconds(attempt, base_seconds=2.0, factor=2.0, max_seconds=900.0)
        for attempt in range(1, NOTIFICATION_MAX_ATTEMPTS + 1)
    ]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_is_capped():
    assert backoff_delay_seconds(20, base_seconds=2.0, factor=2.0, max_seconds=900.0) == 900.0


def test_attempt_limit_comes_from_the_rule():
    assert NOTIFICATION_MAX_ATTEMPTS == 5


# --- NOC storage and rendering (rules.yaml#EXIT-09) ---------------------------


async def test_noc_storage_refuses_a_non_uae_region(tmp_path):
    with pytest.raises(NocStorageError, match="UAE region"):
        FilesystemNocStorage(tmp_path, "bucket", "eu-west-1")


async def test_stored_noc_cannot_be_overwritten(tmp_path):
    storage = FilesystemNocStorage(tmp_path, "bucket", "me-central-1")
    stored = await storage.put_immutable("a/b.pdf", b"first")
    assert stored.byte_size == 5

    with pytest.raises(ImmutableObjectExists):
        await storage.put_immutable("a/b.pdf", b"second")

    assert await storage.get("a/b.pdf") == b"first"


async def test_storage_key_cannot_escape_the_root(tmp_path):
    storage = FilesystemNocStorage(tmp_path, "bucket", "me-central-1")
    with pytest.raises(NocStorageError):
        await storage.put_immutable("../escaped.pdf", b"x")


def test_rendered_pdf_is_valid_and_deterministic():
    created_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    first = render_pdf(
        title="NO OBJECTION CERTIFICATE",
        lines=["Reference: EX-20260808-00001", "Amount: AED 1,000.00"],
        subject="test",
        created_at=created_at,
    )
    second = render_pdf(
        title="NO OBJECTION CERTIFICATE",
        lines=["Reference: EX-20260808-00001", "Amount: AED 1,000.00"],
        subject="test",
        created_at=created_at,
    )
    assert first == second  # the stored digest must mean something
    assert first.startswith(b"%PDF-1.4")
    assert b"/Type /Catalog" in first
    assert first.rstrip().endswith(b"%%EOF")
    assert b"startxref" in first


def test_pdf_rejects_characters_the_base_font_cannot_encode():
    """blockers.md#B-10 — an Arabic NOC needs an embedded font, which is unspecified."""
    with pytest.raises(PdfRenderError):
        render_pdf(
            title="شهادة عدم ممانعة",
            lines=["x"],
            subject="test",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        )
