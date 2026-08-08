"""Exit workflow schema.

Carries three things application code cannot: the workflow-ID sequence
(rules.yaml#EXIT-02), the append-only enforcement on the audit table (AGENTS.md:
"Audit rows are append-only. Enforced by DB trigger, not application code") and the
same enforcement on issued NOC rows (rules.yaml#EXIT-09, "immutable once issued").

Revision ID: 0001_exit_workflow
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_exit_workflow"
down_revision = None
branch_labels = None
depends_on = None

WORKFLOW_STATES = (
    "INITIATED",
    "DOCS_SUBMITTED",
    "OWNER_NOTIFIED",
    "INSPECTION_SCHEDULED",
    "INSPECTION_DONE",
    "DAMAGE_CONFIRMED",
    "REFUND_PROCESSED",
    "NOC_ISSUED",
    "COMPLETE",
    "STALLED",
)
PAYMENT_STATUSES = ("PENDING", "SUCCEEDED", "FAILED")
OUTBOX_STATUSES = ("PENDING", "SENT", "DEAD_LETTER")
ADMIN_TASK_TYPES = ("EXIT_WORKFLOW_STALLED", "OWNER_NOTIFICATION_DEAD_LETTER")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # rules.yaml#EXIT-02 — "sequence from PostgreSQL".
    op.execute("CREATE SEQUENCE IF NOT EXISTS exit_workflow_id_seq START WITH 1 INCREMENT BY 1")

    op.create_table(
        "properties",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("exit_lock", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("exit_lock_workflow_id", sa.String(32)),
        sa.Column("exit_lock_set_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(exit_lock IS FALSE AND exit_lock_workflow_id IS NULL) "
            "OR (exit_lock IS TRUE AND exit_lock_workflow_id IS NOT NULL)",
            name="exit_lock_has_workflow",
        ),
    )

    op.create_table(
        "contracts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "property_id",
            sa.String(64),
            sa.ForeignKey("properties.id", name="fk_contracts_property_id_properties"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("security_deposit_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AED"),
        sa.CheckConstraint("security_deposit_minor >= 0", name="deposit_non_negative"),
        sa.CheckConstraint("currency = 'AED'", name="currency_is_aed"),
    )

    # rules.yaml#EXIT-08 — DEPOSIT_REFUND with the workflow ID as idempotency key.
    op.create_table(
        "payments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(32), nullable=False, server_default="DEPOSIT_REFUND"),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(32)),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AED"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("gateway_reference", sa.String(128)),
        sa.Column("failure_reason", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        sa.CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        sa.CheckConstraint(_in_list("status", PAYMENT_STATUSES), name="status_known"),
        sa.CheckConstraint("currency = 'AED'", name="currency_is_aed"),
    )
    op.create_index("ix_payments_workflow_id", "payments", ["workflow_id"])

    op.create_table(
        "exit_workflows",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "contract_id",
            sa.String(64),
            sa.ForeignKey("contracts.id", name="fk_exit_workflows_contract_id_contracts"),
            nullable=False,
        ),
        sa.Column(
            "property_id",
            sa.String(64),
            sa.ForeignKey("properties.id", name="fk_exit_workflows_property_id_properties"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        # edges.yaml#X-007 — a Dubai calendar day, DATE not TIMESTAMP.
        sa.Column("move_out_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("documents", postgresql.JSONB(), nullable=False),
        sa.Column("security_deposit_minor", sa.BigInteger(), nullable=False),
        sa.Column("damage_amount_minor", sa.BigInteger()),
        sa.Column("damage_photos", postgresql.JSONB()),
        sa.Column("inspection_reported_at", sa.DateTime(timezone=True)),
        sa.Column("damage_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("refund_amount_minor", sa.BigInteger()),
        sa.Column(
            "payment_id",
            sa.String(64),
            sa.ForeignKey("payments.id", name="fk_exit_workflows_payment_id_payments"),
        ),
        sa.Column("noc_document_id", sa.String(64)),
        sa.Column("stalled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # rules.yaml#EXIT-01, edges.yaml#X-001 — never a second workflow for a contract.
        sa.UniqueConstraint("contract_id", name="uq_exit_workflows_contract_id"),
        sa.CheckConstraint(_in_list("status", WORKFLOW_STATES), name="status_in_states_yaml"),
        sa.CheckConstraint(
            "damage_amount_minor IS NULL OR damage_amount_minor >= 0",
            name="damage_non_negative",
        ),
        sa.CheckConstraint(
            "refund_amount_minor IS NULL OR refund_amount_minor >= 0",
            name="refund_non_negative",
        ),
    )
    op.create_index(
        "ix_exit_workflows_status_move_out_date", "exit_workflows", ["status", "move_out_date"]
    )
    op.create_index("ix_exit_workflows_property_id", "exit_workflows", ["property_id"])

    # rules.yaml#EXIT-10 — append-only, 7 year retention.
    op.create_table(
        "exit_workflow_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_id",
            sa.String(32),
            sa.ForeignKey(
                "exit_workflows.id", name="fk_exit_workflow_audit_workflow_id_exit_workflows"
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(64)),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(32)),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("rule_id", sa.String(16)),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_exit_workflow_audit_workflow_id", "exit_workflow_audit", ["workflow_id"])
    op.create_index("ix_exit_workflow_audit_created_at", "exit_workflow_audit", ["created_at"])

    # rules.yaml#EXIT-09 — the issued NOC.
    op.create_table(
        "noc_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(32),
            sa.ForeignKey("exit_workflows.id", name="fk_noc_documents_workflow_id_exit_workflows"),
            nullable=False,
        ),
        sa.Column("bucket", sa.String(128), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False, server_default="application/pdf"),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workflow_id", name="uq_noc_documents_workflow_id"),
    )

    # rules.yaml#EXIT-04, edges.yaml#X-002 — transactional outbox for owner notification.
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column("workflow_id", sa.String(32)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_in_list("status", OUTBOX_STATUSES), name="status_known"),
    )
    op.create_index("ix_event_outbox_due", "event_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_event_outbox_workflow_id", "event_outbox", ["workflow_id"])

    # rules.yaml#EXIT-05 and #EXIT-04 admin follow-ups.
    op.create_table(
        "admin_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_in_list("type", ADMIN_TASK_TYPES), name="type_known"),
        sa.UniqueConstraint("workflow_id", "type", name="uq_admin_tasks_workflow_id"),
    )
    op.create_index("ix_admin_tasks_workflow_id", "admin_tasks", ["workflow_id"])

    # AGENTS.md: "Audit rows are append-only. Enforced by DB trigger, not application
    # code." rules.yaml#EXIT-10. The same guarantee is applied to issued NOC rows,
    # rules.yaml#EXIT-09 "immutable once issued".
    op.execute(
        """
        CREATE OR REPLACE FUNCTION exit_workflow_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'table % is append-only (%: rules.yaml#EXIT-10 / #EXIT-09)',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER exit_workflow_audit_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON exit_workflow_audit
        FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_append_only();
        """
    )
    op.execute(
        """
        CREATE TRIGGER noc_documents_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON noc_documents
        FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS noc_documents_immutable ON noc_documents")
    op.execute("DROP TRIGGER IF EXISTS exit_workflow_audit_append_only ON exit_workflow_audit")
    op.execute("DROP FUNCTION IF EXISTS exit_workflow_append_only()")
    op.drop_table("admin_tasks")
    op.drop_table("event_outbox")
    op.drop_table("noc_documents")
    op.drop_table("exit_workflow_audit")
    op.drop_table("exit_workflows")
    op.drop_table("payments")
    op.drop_table("contracts")
    op.drop_table("properties")
    op.execute("DROP SEQUENCE IF EXISTS exit_workflow_id_seq")
