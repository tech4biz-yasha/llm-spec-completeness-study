"""Migration/model agreement and the NOC document itself."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from exit_workflow.adapters.noc_pdf import SimpleNocPdfRenderer
from exit_workflow.clock import UTC
from exit_workflow.config import Settings
from exit_workflow.db import models  # noqa: F401 - registers tables
from exit_workflow.db.base import Base
from exit_workflow.ports.renderer import NocContext


def test_migration_matches_the_orm_models(engine):
    """The hand-written migration and the models must not drift apart."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    # alembic reports server_default differences on reflected columns as noise; keep only
    # structural differences.
    structural = [entry for entry in diff if entry[0] not in {"modify_default"}]
    assert structural == [], structural


def test_audit_trigger_exists(engine):
    with engine.connect() as connection:
        triggers = (
            connection.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgrelid IN ('exit_workflow_audit'::regclass, 'noc_documents'::regclass)"
                )
            )
            .scalars()
            .all()
        )
    assert set(triggers) == {"exit_workflow_audit_append_only", "noc_documents_immutable"}


def test_workflow_id_sequence_exists(engine):
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT 1 FROM pg_class WHERE relname = 'exit_workflow_id_seq'")
            ).scalar_one()
            == 1
        )


def _context() -> NocContext:
    return NocContext(
        workflow_id="EX-20260301-00001",
        contract_id="CON-1",
        property_id="PROP-1",
        tenant_id="USR-TENANT-1",
        owner_id="USR-OWNER-1",
        move_out_date=date(2026, 3, 8),
        refund_amount=Decimal("8499.50"),
        currency="AED",
        payment_id="PAY-1",
        payment_reference="gw-1",
        issued_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
    )


def test_noc_is_a_pdf():
    """rules.yaml#EXIT-09 — "NOC is a PDF"."""
    document = SimpleNocPdfRenderer().render(_context())
    assert document.startswith(b"%PDF-1.4")
    assert document.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in document
    assert b"startxref" in document


def test_noc_carries_the_workflow_facts():
    document = SimpleNocPdfRenderer().render(_context())
    for expected in (b"EX-20260301-00001", b"CON-1", b"AED 8499.50", b"PAY-1", b"2026-03-08"):
        assert expected in document


def test_noc_rendering_is_deterministic():
    """A stable byte stream is what makes the stored sha256 meaningful."""
    renderer = SimpleNocPdfRenderer()
    assert renderer.render(_context()) == renderer.render(_context())


def test_noc_bucket_region_must_be_in_the_uae():
    """rules.yaml#EXIT-09."""
    with pytest.raises(ValueError):
        Settings(noc_bucket_region="eu-west-1")
    assert Settings(noc_bucket_region="me-central-1").noc_bucket_region == "me-central-1"


def test_app_refuses_a_non_uae_storage_region(
    session_factory, engine, settings, clock, reasons, gateway, publisher
):
    from exit_workflow.adapters.storage import InMemoryObjectStorage
    from exit_workflow.app import build_app

    with pytest.raises(ValueError):
        build_app(
            reasons=reasons,
            gateway=gateway,
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            clock=clock,
            storage=InMemoryObjectStorage(region="eu-west-1"),
            publisher=publisher,
        )
