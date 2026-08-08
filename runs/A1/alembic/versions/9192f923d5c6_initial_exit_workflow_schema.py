"""initial exit workflow schema

Revision ID: 9192f923d5c6
Revises: 
Create Date: 2026-08-08 13:37:00.087403
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
revision: str = '9192f923d5c6'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reference-number sequences. Alembic's autogenerate does not detect standalone
    # sequences, so they are declared explicitly here (see app/models/sequences.py).
    op.execute(sa.schema.CreateSequence(sa.Sequence('exit_workflow_reference_seq')))
    op.execute(sa.schema.CreateSequence(sa.Sequence('exit_noc_number_seq')))

    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('action', sa.Enum('WORKFLOW_INITIATED', 'DOCUMENT_UPLOADED', 'DOCUMENT_DELETED', 'WORKFLOW_SUBMITTED', 'OWNER_APPROVED', 'OWNER_REJECTED', 'INSPECTION_REQUESTED', 'INSPECTION_SLOTS_PROPOSED', 'INSPECTION_SCHEDULED', 'INSPECTION_COMPLETED', 'DAMAGE_REPORT_SUBMITTED', 'SETTLEMENT_COMPUTED', 'SETTLEMENT_APPROVED', 'SETTLEMENT_DISPUTED', 'PAYMENT_INITIATED', 'PAYMENT_SUCCEEDED', 'PAYMENT_FAILED', 'SETTLEMENT_CLOSED', 'NOC_ISSUED', 'NOC_DOWNLOADED', 'WORKFLOW_COMPLETED', 'WORKFLOW_CANCELLED', 'CONTRACT_BLOCKED', 'CONTRACT_CREATED', name='audit_action'), nullable=False),
    sa.Column('actor_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('entity_type', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=True),
    sa.Column('workflow_id', sa.UUID(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('retain_until', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_log_entity', 'audit_log', ['entity_type', 'entity_id'], unique=False)
    op.create_index('ix_audit_log_occurred_at', 'audit_log', ['occurred_at'], unique=False)
    op.create_index('ix_audit_log_retain_until', 'audit_log', ['retain_until'], unique=False)
    op.create_index('ix_audit_log_workflow', 'audit_log', ['workflow_id', 'occurred_at'], unique=False)
    op.create_table('inspection_agencies',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=True),
    sa.Column('trade_license_number', sa.String(length=64), nullable=True),
    sa.Column('api_key_hash', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('api_key_hash'),
    sa.UniqueConstraint('email')
    )
    op.create_table('outbox_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('topic', sa.String(length=200), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('aggregate_type', sa.String(length=64), nullable=False),
    sa.Column('aggregate_id', sa.UUID(), nullable=False),
    sa.Column('partition_key', sa.String(length=128), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.CheckConstraint('attempts >= 0', name='ck_outbox_attempts_non_negative'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_outbox_events_aggregate', 'outbox_events', ['aggregate_type', 'aggregate_id'], unique=False)
    op.create_index('ix_outbox_events_unpublished', 'outbox_events', ['created_at'], unique=False, postgresql_where=sa.text('published_at IS NULL'))
    op.create_table('owners',
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('tenants',
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=True),
    sa.Column('emirates_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('properties',
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=False),
    sa.Column('address_line', sa.String(length=300), nullable=False),
    sa.Column('community', sa.String(length=120), nullable=True),
    sa.Column('city', sa.String(length=80), nullable=False),
    sa.Column('emirate', sa.String(length=80), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['owners.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    op.create_index(op.f('ix_properties_owner_id'), 'properties', ['owner_id'], unique=False)
    op.create_table('contracts',
    sa.Column('contract_number', sa.String(length=64), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'TERMINATED', 'EXPIRED', name='contract_status'), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('security_deposit_fils', sa.BigInteger(), nullable=False),
    sa.Column('annual_rent_fils', sa.BigInteger(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('annual_rent_fils >= 0', name='ck_contracts_rent_non_negative'),
    sa.CheckConstraint('end_date > start_date', name='ck_contracts_date_order'),
    sa.CheckConstraint('security_deposit_fils >= 0', name='ck_contracts_deposit_non_negative'),
    sa.ForeignKeyConstraint(['owner_id'], ['owners.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contract_number')
    )
    op.create_index(op.f('ix_contracts_owner_id'), 'contracts', ['owner_id'], unique=False)
    op.create_index(op.f('ix_contracts_property_id'), 'contracts', ['property_id'], unique=False)
    op.create_index('ix_contracts_property_status', 'contracts', ['property_id', 'status'], unique=False)
    op.create_index(op.f('ix_contracts_tenant_id'), 'contracts', ['tenant_id'], unique=False)
    op.create_index('ix_contracts_tenant_status', 'contracts', ['tenant_id', 'status'], unique=False)
    op.create_table('exit_workflows',
    sa.Column('reference', sa.String(length=32), nullable=False),
    sa.Column('contract_id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('state', sa.Enum('DRAFT', 'DOCUMENTS_PENDING', 'PENDING_OWNER_APPROVAL', 'OWNER_APPROVED', 'INSPECTION_SCHEDULING', 'INSPECTION_SCHEDULED', 'INSPECTION_COMPLETED', 'DAMAGE_REVIEW', 'PENDING_SETTLEMENT', 'SETTLED', 'NOC_ISSUED', 'COMPLETED', 'CANCELLED', 'REJECTED', name='exit_workflow_state'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('move_out_date', sa.Date(), nullable=False),
    sa.Column('reason_code', sa.Enum('LEASE_EXPIRY', 'EARLY_TERMINATION', 'RELOCATION', 'JOB_CHANGE', 'PROPERTY_PURCHASED', 'LANDLORD_REQUEST', 'OTHER', name='exit_reason_code'), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=True),
    sa.Column('deposit_snapshot_fils', sa.BigInteger(), nullable=False),
    sa.Column('initiated_by_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('owner_approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('inspection_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('noc_issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_reason', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(state IN ('DAMAGE_REVIEW', 'DOCUMENTS_PENDING', 'DRAFT', 'INSPECTION_COMPLETED', 'INSPECTION_SCHEDULED', 'INSPECTION_SCHEDULING', 'NOC_ISSUED', 'OWNER_APPROVED', 'PENDING_OWNER_APPROVAL', 'PENDING_SETTLEMENT', 'SETTLED')) = is_active", name='ck_exit_workflows_is_active_matches_state'),
    sa.CheckConstraint("reason_code <> 'OTHER' OR reason_text IS NOT NULL", name='ck_exit_workflows_other_reason_needs_text'),
    sa.CheckConstraint('deposit_snapshot_fils >= 0', name='ck_exit_workflows_deposit_non_negative'),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['owners.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    op.create_index('ix_exit_workflows_owner_state', 'exit_workflows', ['owner_id', 'state'], unique=False)
    op.create_index('ix_exit_workflows_tenant_active', 'exit_workflows', ['tenant_id', 'is_active'], unique=False)
    op.create_index('uq_exit_workflows_active_contract', 'exit_workflows', ['contract_id'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_index('uq_exit_workflows_active_property', 'exit_workflows', ['property_id'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_table('exit_documents',
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Enum('EMIRATES_ID', 'PASSPORT', 'TENANCY_CONTRACT', 'DEWA_FINAL_BILL', 'KEYS_HANDOVER', 'CLEARANCE_LETTER', 'OTHER', name='exit_document_kind'), nullable=False),
    sa.Column('file_name', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=120), nullable=False),
    sa.Column('byte_size', sa.BigInteger(), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
    sa.Column('uploaded_by_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=False),
    sa.Column('uploaded_by_id', sa.UUID(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('byte_size > 0', name='ck_exit_documents_size_positive'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('storage_key', name='uq_exit_documents_storage_key')
    )
    op.create_index('ix_exit_documents_workflow_kind', 'exit_documents', ['workflow_id', 'kind'], unique=False)
    op.create_table('exit_workflow_transitions',
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('from_state', sa.Enum('DRAFT', 'DOCUMENTS_PENDING', 'PENDING_OWNER_APPROVAL', 'OWNER_APPROVED', 'INSPECTION_SCHEDULING', 'INSPECTION_SCHEDULED', 'INSPECTION_COMPLETED', 'DAMAGE_REVIEW', 'PENDING_SETTLEMENT', 'SETTLED', 'NOC_ISSUED', 'COMPLETED', 'CANCELLED', 'REJECTED', name='exit_workflow_state'), nullable=False),
    sa.Column('to_state', sa.Enum('DRAFT', 'DOCUMENTS_PENDING', 'PENDING_OWNER_APPROVAL', 'OWNER_APPROVED', 'INSPECTION_SCHEDULING', 'INSPECTION_SCHEDULED', 'INSPECTION_COMPLETED', 'DAMAGE_REVIEW', 'PENDING_SETTLEMENT', 'SETTLED', 'NOC_ISSUED', 'COMPLETED', 'CANCELLED', 'REJECTED', name='exit_workflow_state'), nullable=False),
    sa.Column('actor_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_exit_workflow_transitions_workflow', 'exit_workflow_transitions', ['workflow_id', 'occurred_at'], unique=False)
    op.create_table('inspection_assignments',
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('agency_id', sa.UUID(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('REQUESTED', 'SLOTS_PROPOSED', 'SCHEDULED', 'COMPLETED', 'CANCELLED', name='assignment_status'), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scheduled_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scheduled_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_reason', sa.Text(), nullable=True),
    sa.Column('instructions', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('attempt >= 1', name='ck_inspection_assignments_attempt_positive'),
    sa.ForeignKeyConstraint(['agency_id'], ['inspection_agencies.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id', 'attempt', name='uq_inspection_assignment_attempt')
    )
    op.create_index('ix_inspection_assignments_agency_status', 'inspection_assignments', ['agency_id', 'status'], unique=False)
    op.create_table('damage_reports',
    sa.Column('assignment_id', sa.UUID(), nullable=False),
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('agency_id', sa.UUID(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('inspector_name', sa.String(length=200), nullable=True),
    sa.Column('inspected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('total_deductions_fils', sa.BigInteger(), nullable=False),
    sa.Column('photos', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('total_deductions_fils >= 0', name='ck_damage_reports_total_non_negative'),
    sa.ForeignKeyConstraint(['agency_id'], ['inspection_agencies.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['assignment_id'], ['inspection_assignments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('assignment_id', name='uq_damage_reports_assignment')
    )
    op.create_index('ix_damage_reports_workflow', 'damage_reports', ['workflow_id'], unique=False)
    op.create_table('inspection_slots',
    sa.Column('assignment_id', sa.UUID(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('is_selected', sa.Boolean(), nullable=False),
    sa.Column('selected_by_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=True),
    sa.Column('selected_by_id', sa.UUID(), nullable=True),
    sa.Column('selected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('proposed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('ends_at > starts_at', name='ck_inspection_slots_time_order'),
    sa.ForeignKeyConstraint(['assignment_id'], ['inspection_assignments.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('assignment_id', 'starts_at', 'ends_at', name='uq_inspection_slot_window')
    )
    op.create_index('uq_inspection_slots_selected', 'inspection_slots', ['assignment_id'], unique=True, postgresql_where=sa.text('is_selected'))
    op.create_table('damage_line_items',
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('location', sa.String(length=120), nullable=True),
    sa.Column('severity', sa.Enum('MINOR', 'MODERATE', 'MAJOR', 'FAIR_WEAR_AND_TEAR', name='damage_severity'), nullable=False),
    sa.Column('amount_fils', sa.BigInteger(), nullable=False),
    sa.Column('photos', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('amount_fils >= 0', name='ck_damage_line_items_amount_non_negative'),
    sa.ForeignKeyConstraint(['report_id'], ['damage_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_damage_line_items_report', 'damage_line_items', ['report_id'], unique=False)
    op.create_table('deposit_settlements',
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('damage_report_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.Enum('DRAFT', 'PAYABLE', 'CLOSED', 'VOID', name='settlement_status'), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('deposit_fils', sa.BigInteger(), nullable=False),
    sa.Column('total_deductions_fils', sa.BigInteger(), nullable=False),
    sa.Column('refund_fils', sa.BigInteger(), nullable=False),
    sa.Column('balance_due_fils', sa.BigInteger(), nullable=False),
    sa.Column('refund_settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('balance_settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=True),
    sa.Column('approved_by_id', sa.UUID(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('balance_due_fils >= 0', name='ck_settlement_balance_non_negative'),
    sa.CheckConstraint('deposit_fils >= 0', name='ck_settlement_deposit_non_negative'),
    sa.CheckConstraint('refund_fils - balance_due_fils = deposit_fils - total_deductions_fils', name='ck_settlement_balances'),
    sa.CheckConstraint('refund_fils = 0 OR balance_due_fils = 0', name='ck_settlement_single_sided'),
    sa.CheckConstraint('refund_fils >= 0', name='ck_settlement_refund_non_negative'),
    sa.CheckConstraint('total_deductions_fils >= 0', name='ck_settlement_deductions_non_negative'),
    sa.ForeignKeyConstraint(['damage_report_id'], ['damage_reports.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id', name='uq_deposit_settlements_workflow')
    )
    op.create_table('exit_nocs',
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('settlement_id', sa.UUID(), nullable=False),
    sa.Column('noc_number', sa.String(length=32), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('pdf_bytes', sa.LargeBinary(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.Integer(), nullable=False),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('download_count', sa.Integer(), nullable=False),
    sa.Column('first_downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('byte_size > 0', name='ck_exit_nocs_size_positive'),
    sa.CheckConstraint('download_count >= 0', name='ck_exit_nocs_download_count'),
    sa.ForeignKeyConstraint(['settlement_id'], ['deposit_settlements.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('noc_number', name='uq_exit_nocs_number'),
    sa.UniqueConstraint('workflow_id', name='uq_exit_nocs_workflow')
    )
    op.create_index(op.f('ix_exit_nocs_content_sha256'), 'exit_nocs', ['content_sha256'], unique=False)
    op.create_table('payment_transactions',
    sa.Column('settlement_id', sa.UUID(), nullable=False),
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('leg', sa.Enum('OWNER_REFUND', 'TENANT_BALANCE', name='payment_leg'), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'SUCCEEDED', 'FAILED', name='payment_status'), nullable=False),
    sa.Column('amount_fils', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('provider_reference', sa.String(length=128), nullable=True),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column('initiated_by_type', sa.Enum('TENANT', 'OWNER', 'AGENCY', 'ADMIN', 'SYSTEM', name='actor_type'), nullable=False),
    sa.Column('initiated_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.CheckConstraint('amount_fils >= 0', name='ck_payment_amount_non_negative'),
    sa.ForeignKeyConstraint(['settlement_id'], ['deposit_settlements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workflow_id'], ['exit_workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key', name='uq_payment_transactions_idempotency_key')
    )
    op.create_index('ix_payment_transactions_workflow', 'payment_transactions', ['workflow_id'], unique=False)
    op.create_index('uq_payment_transactions_succeeded_leg', 'payment_transactions', ['settlement_id', 'leg'], unique=True, postgresql_where=sa.text("status = 'SUCCEEDED'"))


def downgrade() -> None:
    op.drop_index('uq_payment_transactions_succeeded_leg', table_name='payment_transactions', postgresql_where=sa.text("status = 'SUCCEEDED'"))
    op.drop_index('ix_payment_transactions_workflow', table_name='payment_transactions')
    op.drop_table('payment_transactions')
    op.drop_index(op.f('ix_exit_nocs_content_sha256'), table_name='exit_nocs')
    op.drop_table('exit_nocs')
    op.drop_table('deposit_settlements')
    op.drop_index('ix_damage_line_items_report', table_name='damage_line_items')
    op.drop_table('damage_line_items')
    op.drop_index('uq_inspection_slots_selected', table_name='inspection_slots', postgresql_where=sa.text('is_selected'))
    op.drop_table('inspection_slots')
    op.drop_index('ix_damage_reports_workflow', table_name='damage_reports')
    op.drop_table('damage_reports')
    op.drop_index('ix_inspection_assignments_agency_status', table_name='inspection_assignments')
    op.drop_table('inspection_assignments')
    op.drop_index('ix_exit_workflow_transitions_workflow', table_name='exit_workflow_transitions')
    op.drop_table('exit_workflow_transitions')
    op.drop_index('ix_exit_documents_workflow_kind', table_name='exit_documents')
    op.drop_table('exit_documents')
    op.drop_index('uq_exit_workflows_active_property', table_name='exit_workflows', postgresql_where=sa.text('is_active'))
    op.drop_index('uq_exit_workflows_active_contract', table_name='exit_workflows', postgresql_where=sa.text('is_active'))
    op.drop_index('ix_exit_workflows_tenant_active', table_name='exit_workflows')
    op.drop_index('ix_exit_workflows_owner_state', table_name='exit_workflows')
    op.drop_table('exit_workflows')
    op.drop_index('ix_contracts_tenant_status', table_name='contracts')
    op.drop_index(op.f('ix_contracts_tenant_id'), table_name='contracts')
    op.drop_index('ix_contracts_property_status', table_name='contracts')
    op.drop_index(op.f('ix_contracts_property_id'), table_name='contracts')
    op.drop_index(op.f('ix_contracts_owner_id'), table_name='contracts')
    op.drop_table('contracts')
    op.drop_index(op.f('ix_properties_owner_id'), table_name='properties')
    op.drop_table('properties')
    op.drop_table('tenants')
    op.drop_table('owners')
    op.drop_index('ix_outbox_events_unpublished', table_name='outbox_events', postgresql_where=sa.text('published_at IS NULL'))
    op.drop_index('ix_outbox_events_aggregate', table_name='outbox_events')
    op.drop_table('outbox_events')
    op.drop_table('inspection_agencies')
    op.drop_index('ix_audit_log_workflow', table_name='audit_log')
    op.drop_index('ix_audit_log_retain_until', table_name='audit_log')
    op.drop_index('ix_audit_log_occurred_at', table_name='audit_log')
    op.drop_index('ix_audit_log_entity', table_name='audit_log')
    op.drop_table('audit_log')

    # op.drop_table() leaves PostgreSQL ENUM types behind, so a downgrade followed by an
    # upgrade would fail on "type already exists". Drop them explicitly.
    for enum_name in (
        "audit_action",
        "actor_type",
        "contract_status",
        "exit_workflow_state",
        "exit_reason_code",
        "exit_document_kind",
        "assignment_status",
        "damage_severity",
        "settlement_status",
        "payment_leg",
        "payment_status",
    ):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
    op.execute(sa.schema.DropSequence(sa.Sequence('exit_noc_number_seq')))
    op.execute(sa.schema.DropSequence(sa.Sequence('exit_workflow_reference_seq')))
