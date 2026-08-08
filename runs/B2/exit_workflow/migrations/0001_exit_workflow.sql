-- Exit workflow module schema.
-- Traceability: rules.yaml#EXIT-01..EXIT-10, states.yaml#exit_workflow, edges.yaml.

-- ---------------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------------

-- states.yaml#exit_workflow.states — verbatim, in declaration order.
DO $$ BEGIN
    CREATE TYPE exit_workflow_state AS ENUM (
        'INITIATED', 'DOCS_SUBMITTED', 'OWNER_NOTIFIED', 'INSPECTION_SCHEDULED',
        'INSPECTION_DONE', 'DAMAGE_CONFIRMED', 'REFUND_PROCESSED', 'NOC_ISSUED',
        'COMPLETE', 'STALLED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- rules.yaml#EXIT-08 — refund is a payment transaction of type DEPOSIT_REFUND.
DO $$ BEGIN
    CREATE TYPE payment_type AS ENUM ('DEPOSIT_REFUND');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- algorithm.md step 11 — the gateway outcomes this module reacts to.
DO $$ BEGIN
    CREATE TYPE payment_status AS ENUM ('PENDING', 'SUCCEEDED', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE outbox_status AS ENUM ('PENDING', 'PUBLISHED', 'DEAD_LETTERED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Workflow id sequence — rules.yaml#EXIT-02 "sequence from PostgreSQL"
-- blockers.md#B-008: daily reset vs global is unspecified; global is used.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS exit_workflow_id_seq AS bigint START WITH 1 NO CYCLE;

-- ---------------------------------------------------------------------------
-- Workflow
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflows (
    -- rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, server assigned.
    id                      text PRIMARY KEY CHECK (id ~ '^EX-[0-9]{8}-[0-9]{5}$'),
    contract_id             uuid NOT NULL,
    property_id             uuid NOT NULL,
    tenant_id               uuid NOT NULL,
    owner_id                uuid NOT NULL,
    status                  exit_workflow_state NOT NULL,

    -- edges.yaml#X-007 — Dubai calendar day, stored as date, never datetime.
    move_out_date           date NOT NULL,
    reason                  text NOT NULL,
    documents               jsonb NOT NULL,

    -- Snapshot of the deposit at initiation, minor units (rules.yaml#EXIT-07).
    security_deposit_minor  bigint NOT NULL CHECK (security_deposit_minor >= 0),

    inspection_scheduled_at timestamptz,
    inspection_scheduled_for date,

    -- rules.yaml#EXIT-06 — entered by the inspection agency, then confirmed by the owner.
    damage_amount_minor     bigint CHECK (damage_amount_minor >= 0),
    inspection_photos       jsonb,
    inspection_reported_at  timestamptz,
    confirmed_damage_minor  bigint CHECK (confirmed_damage_minor >= 0),
    damage_confirmed_at     timestamptz,
    dispute_count           integer NOT NULL DEFAULT 0 CHECK (dispute_count >= 0),

    -- rules.yaml#EXIT-07 / EXIT-08
    refund_amount_minor     bigint CHECK (refund_amount_minor >= 0),
    payment_id              uuid,
    -- rules.yaml#EXIT-09
    noc_document_id         uuid,

    stalled_at              timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    version                 integer NOT NULL DEFAULT 1
);

-- rules.yaml#EXIT-01 / edges.yaml#X-001 — "only one exit workflow may exist per
-- contract at any time". The lock is released by COMPLETE (rules.yaml#EXIT-03),
-- so completed workflows do not block a future exit; everything else does.
-- This index is what makes a duplicate initiation impossible under concurrency,
-- not the application-level pre-check.
CREATE UNIQUE INDEX IF NOT EXISTS uq_exit_workflow_open_per_contract
    ON exit_workflows (contract_id)
    WHERE status <> 'COMPLETE';

CREATE INDEX IF NOT EXISTS ix_exit_workflows_property ON exit_workflows (property_id);
CREATE INDEX IF NOT EXISTS ix_exit_workflows_tenant ON exit_workflows (tenant_id);
-- Supports the EXIT-05 stall sweep.
CREATE INDEX IF NOT EXISTS ix_exit_workflows_stall_scan
    ON exit_workflows (status, move_out_date)
    WHERE status IN ('OWNER_NOTIFIED', 'INSPECTION_SCHEDULED');

-- ---------------------------------------------------------------------------
-- Payments — rules.yaml#EXIT-08, edges.yaml#X-005
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                  uuid PRIMARY KEY,
    workflow_id         text NOT NULL REFERENCES exit_workflows (id),
    type                payment_type NOT NULL,
    amount_minor        bigint NOT NULL CHECK (amount_minor >= 0),
    currency            char(3) NOT NULL DEFAULT 'AED',
    status              payment_status NOT NULL DEFAULT 'PENDING',
    -- rules.yaml#EXIT-08 — idempotency key IS the workflow id. The unique
    -- constraint is the concurrency control for edges.yaml#X-005.
    idempotency_key     text NOT NULL UNIQUE,
    gateway_reference   text,
    failure_reason      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- NOC documents — rules.yaml#EXIT-09, immutable once issued
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS noc_documents (
    id              uuid PRIMARY KEY,
    workflow_id     text NOT NULL UNIQUE REFERENCES exit_workflows (id),
    bucket          text NOT NULL,
    object_key      text NOT NULL,
    -- rules.yaml#EXIT-09 — UAE region bucket. Enforced in application config too.
    region          text NOT NULL,
    content_sha256  char(64) NOT NULL,
    size_bytes      integer NOT NULL CHECK (size_bytes > 0),
    issued_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Audit — rules.yaml#EXIT-10. Append-only, 7 year retention.
-- AGENTS.md: "Audit rows are append-only. Enforced by DB trigger, not
-- application code."
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflow_audit (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id     text NOT NULL,
    actor_id        text NOT NULL,
    actor_role      text NOT NULL,
    from_state      exit_workflow_state,
    to_state        exit_workflow_state NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

-- rules.yaml#EXIT-10 — 7 year retention. Nothing may delete an audit row before
-- occurred_at + 7 years, and the trigger below blocks deletion outright: a purge
-- job has to disable the trigger under a privileged role, which is the point.
COMMENT ON TABLE exit_workflow_audit IS
    'Append-only exit workflow audit (rules.yaml#EXIT-10). Retention: 7 years from occurred_at.';

CREATE INDEX IF NOT EXISTS ix_exit_workflow_audit_workflow
    ON exit_workflow_audit (workflow_id, occurred_at);

CREATE OR REPLACE FUNCTION exit_workflow_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'relation %.% is append-only and immutable (rules.yaml#EXIT-09, #EXIT-10)',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_exit_workflow_audit_append_only ON exit_workflow_audit;
CREATE TRIGGER trg_exit_workflow_audit_append_only
    BEFORE UPDATE OR DELETE ON exit_workflow_audit
    FOR EACH ROW EXECUTE FUNCTION exit_workflow_reject_mutation();

DROP TRIGGER IF EXISTS trg_exit_workflow_audit_no_truncate ON exit_workflow_audit;
CREATE TRIGGER trg_exit_workflow_audit_no_truncate
    BEFORE TRUNCATE ON exit_workflow_audit
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_reject_mutation();

-- rules.yaml#EXIT-09 — the NOC is immutable once issued. Same guard.
DROP TRIGGER IF EXISTS trg_noc_documents_immutable ON noc_documents;
CREATE TRIGGER trg_noc_documents_immutable
    BEFORE UPDATE OR DELETE ON noc_documents
    FOR EACH ROW EXECUTE FUNCTION exit_workflow_reject_mutation();

DROP TRIGGER IF EXISTS trg_noc_documents_no_truncate ON noc_documents;
CREATE TRIGGER trg_noc_documents_no_truncate
    BEFORE TRUNCATE ON noc_documents
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_reject_mutation();

-- ---------------------------------------------------------------------------
-- Event outbox — rules.yaml#EXIT-04, edges.yaml#X-002
-- The event row is written inside the initiation transaction so it cannot be
-- lost; it is PUBLISHED after that transaction commits. A dispatch failure
-- never rolls the workflow back: 5 attempts with exponential backoff, then
-- DEAD_LETTERED plus an admin task.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflow_events (
    id              uuid PRIMARY KEY,
    workflow_id     text NOT NULL REFERENCES exit_workflows (id),
    topic           text NOT NULL,
    event_type      text NOT NULL,
    partition_key   text NOT NULL,
    payload         jsonb NOT NULL,
    status          outbox_status NOT NULL DEFAULT 'PENDING',
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz
);

CREATE INDEX IF NOT EXISTS ix_exit_workflow_events_due
    ON exit_workflow_events (next_attempt_at)
    WHERE status = 'PENDING';

-- ---------------------------------------------------------------------------
-- Admin tasks — rules.yaml#EXIT-05 (stall) and EXIT-04 (dead-letter alert)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflow_admin_tasks (
    id              uuid PRIMARY KEY,
    workflow_id     text NOT NULL REFERENCES exit_workflows (id),
    task_type       text NOT NULL,
    status          text NOT NULL DEFAULT 'OPEN',
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- One open task of a given type per workflow; the stall sweep and the
-- dead-letter handler are both at-least-once.
CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_task_open
    ON exit_workflow_admin_tasks (workflow_id, task_type)
    WHERE status = 'OPEN';
