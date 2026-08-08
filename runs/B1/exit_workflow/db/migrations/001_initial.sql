-- Exit workflow module — initial schema.
--
-- Traces to: rules.yaml#EXIT-01..EXIT-10, states.yaml#exit_workflow,
-- edges.yaml#X-001, #X-005, #X-006, AGENTS.md (money in minor units, UTC
-- timestamps, audit append-only enforced by DB trigger).
--
-- Idempotent: safe to run against a database that already has the schema.

BEGIN;

-- ---------------------------------------------------------------------------
-- Append-only / immutability enforcement.
--
-- AGENTS.md: "Audit rows are append-only. Enforced by DB trigger, not
-- application code." Statement-level so it fires even for a zero-row UPDATE,
-- and it covers TRUNCATE, which row-level triggers do not see.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION exit_workflow_refuse_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'relation % is append-only; % is not permitted (rules.yaml#EXIT-10, #EXIT-09)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = '0A000';  -- feature_not_supported
END;
$$;

-- ---------------------------------------------------------------------------
-- Properties and contracts are owned by other modules; created here only so
-- this module can be deployed and tested standalone. In the monolith schema
-- these statements are no-ops because the tables already exist.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS properties (
    id                     UUID PRIMARY KEY,
    owner_id               UUID        NOT NULL,
    -- rules.yaml#EXIT-03: set true with the workflow insert, released by COMPLETE.
    exit_lock              BOOLEAN     NOT NULL DEFAULT FALSE,
    exit_lock_workflow_id  VARCHAR(20),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contracts (
    id                      UUID PRIMARY KEY,
    property_id             UUID        NOT NULL REFERENCES properties (id),
    tenant_id               UUID        NOT NULL,
    owner_id                UUID        NOT NULL,
    status                  VARCHAR(32) NOT NULL,
    -- AGENTS.md: money is minor units (fils) in storage.
    security_deposit_minor  BIGINT      NOT NULL,
    currency                VARCHAR(3)  NOT NULL DEFAULT 'AED',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_contracts_deposit_non_negative CHECK (security_deposit_minor >= 0),
    CONSTRAINT ck_contracts_currency_aed         CHECK (currency = 'AED')
);

-- ---------------------------------------------------------------------------
-- Payments. rules.yaml#EXIT-08 — DEPOSIT_REFUND, idempotency key = workflow ID.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                        UUID PRIMARY KEY,
    payment_type              VARCHAR(32) NOT NULL,
    workflow_id               VARCHAR(20),
    contract_id               UUID,
    payee_id                  UUID        NOT NULL,
    amount_minor              BIGINT      NOT NULL,
    currency                  VARCHAR(3)  NOT NULL DEFAULT 'AED',
    status                    VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    -- edges.yaml#X-005: one payment per workflow even under a settlement race.
    idempotency_key           VARCHAR(64) NOT NULL,
    gateway_reference         VARCHAR(128),
    gateway_status_checked_at TIMESTAMPTZ,
    settled_at                TIMESTAMPTZ,
    failure_reason            TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_payments_idempotency_key     UNIQUE (idempotency_key),
    CONSTRAINT ck_payments_amount_non_negative CHECK (amount_minor >= 0),
    CONSTRAINT ck_payments_currency_aed        CHECK (currency = 'AED'),
    CONSTRAINT ck_payments_status              CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_payments_type                CHECK (payment_type IN ('DEPOSIT_REFUND'))
);

-- ---------------------------------------------------------------------------
-- Workflow ID counter. rules.yaml#EXIT-02 — "sequence from PostgreSQL".
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS exit_workflow_number_seq START WITH 1 INCREMENT BY 1;

-- ---------------------------------------------------------------------------
-- The workflow document.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflows (
    -- rules.yaml#EXIT-02: EX-YYYYMMDD-NNNNN.
    id                      VARCHAR(20) PRIMARY KEY,
    -- rules.yaml#EXIT-01 / edges.yaml#X-001: exactly one workflow per contract.
    -- Enforced by the database so a duplicate-initiation race cannot win.
    contract_id             UUID        NOT NULL REFERENCES contracts (id),
    property_id             UUID        NOT NULL REFERENCES properties (id),
    tenant_id               UUID        NOT NULL,
    owner_id                UUID        NOT NULL,
    status                  VARCHAR(32) NOT NULL,

    -- edges.yaml#X-007: Asia/Dubai calendar day, stored as DATE not TIMESTAMP.
    move_out_date           DATE        NOT NULL,
    reason                  VARCHAR(64) NOT NULL,
    documents               JSONB       NOT NULL,
    security_deposit_minor  BIGINT      NOT NULL,

    inspection_scheduled_at TIMESTAMPTZ,
    damage_amount_minor     BIGINT,
    damage_photos           JSONB,
    inspection_reported_at  TIMESTAMPTZ,
    damage_confirmed_at     TIMESTAMPTZ,

    refund_amount_minor     BIGINT,
    payment_id              UUID UNIQUE REFERENCES payments (id),

    noc_document_id         UUID UNIQUE,
    noc_issued_at           TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    stalled_at              TIMESTAMPTZ,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_exit_workflows_contract UNIQUE (contract_id),
    -- states.yaml#exit_workflow.states, verbatim.
    CONSTRAINT ck_exit_workflows_status CHECK (status IN (
        'INITIATED', 'DOCS_SUBMITTED', 'OWNER_NOTIFIED', 'INSPECTION_SCHEDULED',
        'INSPECTION_DONE', 'DAMAGE_CONFIRMED', 'REFUND_PROCESSED', 'NOC_ISSUED',
        'COMPLETE', 'STALLED')),
    -- rules.yaml#EXIT-02: at least one document.
    CONSTRAINT ck_exit_workflows_documents_required CHECK (
        jsonb_typeof(documents) = 'array' AND jsonb_array_length(documents) >= 1),
    CONSTRAINT ck_exit_workflows_damage_non_negative CHECK (
        damage_amount_minor IS NULL OR damage_amount_minor >= 0),
    CONSTRAINT ck_exit_workflows_refund_non_negative CHECK (
        refund_amount_minor IS NULL OR refund_amount_minor >= 0),
    CONSTRAINT ck_exit_workflows_deposit_non_negative CHECK (security_deposit_minor >= 0)
);

CREATE INDEX IF NOT EXISTS ix_exit_workflows_property
    ON exit_workflows (property_id);
CREATE INDEX IF NOT EXISTS ix_exit_workflows_tenant
    ON exit_workflows (tenant_id);
-- Drives the stall scan (rules.yaml#EXIT-05).
CREATE INDEX IF NOT EXISTS ix_exit_workflows_status_move_out
    ON exit_workflows (status, move_out_date);

-- ---------------------------------------------------------------------------
-- Audit. rules.yaml#EXIT-10 — actor, timestamp, from, to, metadata;
-- append-only, 7 year retention.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exit_workflow_audit (
    id          BIGSERIAL PRIMARY KEY,
    workflow_id VARCHAR(20) NOT NULL,
    actor_type  VARCHAR(32) NOT NULL,
    actor_id    VARCHAR(64),
    from_state  VARCHAR(32),
    to_state    VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rule_id     VARCHAR(16),
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_audit_to_state CHECK (to_state IN (
        'INITIATED', 'DOCS_SUBMITTED', 'OWNER_NOTIFIED', 'INSPECTION_SCHEDULED',
        'INSPECTION_DONE', 'DAMAGE_CONFIRMED', 'REFUND_PROCESSED', 'NOC_ISSUED',
        'COMPLETE', 'STALLED')),
    CONSTRAINT ck_audit_from_state CHECK (from_state IS NULL OR from_state IN (
        'INITIATED', 'DOCS_SUBMITTED', 'OWNER_NOTIFIED', 'INSPECTION_SCHEDULED',
        'INSPECTION_DONE', 'DAMAGE_CONFIRMED', 'REFUND_PROCESSED', 'NOC_ISSUED',
        'COMPLETE', 'STALLED'))
);

CREATE INDEX IF NOT EXISTS ix_exit_workflow_audit_workflow
    ON exit_workflow_audit (workflow_id, occurred_at);

DROP TRIGGER IF EXISTS trg_exit_workflow_audit_append_only ON exit_workflow_audit;
CREATE TRIGGER trg_exit_workflow_audit_append_only
    BEFORE UPDATE OR DELETE ON exit_workflow_audit
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_refuse_mutation();

DROP TRIGGER IF EXISTS trg_exit_workflow_audit_no_truncate ON exit_workflow_audit;
CREATE TRIGGER trg_exit_workflow_audit_no_truncate
    BEFORE TRUNCATE ON exit_workflow_audit
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_refuse_mutation();

-- ---------------------------------------------------------------------------
-- NOC documents. rules.yaml#EXIT-09 — immutable once issued.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS noc_documents (
    id              UUID PRIMARY KEY,
    workflow_id     VARCHAR(20)  NOT NULL UNIQUE,
    bucket          VARCHAR(128) NOT NULL,
    region          VARCHAR(32)  NOT NULL,
    object_key      VARCHAR(512) NOT NULL,
    content_sha256  VARCHAR(64)  NOT NULL,
    byte_size       INTEGER      NOT NULL,
    issued_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_noc_object UNIQUE (bucket, object_key)
);

DROP TRIGGER IF EXISTS trg_noc_documents_immutable ON noc_documents;
CREATE TRIGGER trg_noc_documents_immutable
    BEFORE UPDATE OR DELETE ON noc_documents
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_refuse_mutation();

DROP TRIGGER IF EXISTS trg_noc_documents_no_truncate ON noc_documents;
CREATE TRIGGER trg_noc_documents_no_truncate
    BEFORE TRUNCATE ON noc_documents
    FOR EACH STATEMENT EXECUTE FUNCTION exit_workflow_refuse_mutation();

-- ---------------------------------------------------------------------------
-- Transactional outbox. rules.yaml#EXIT-04 — emitted after commit, 5 attempts
-- with exponential backoff, then dead-letter plus admin alert.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_outbox (
    id              UUID PRIMARY KEY,
    topic           VARCHAR(255) NOT NULL,
    event_type      VARCHAR(128) NOT NULL,
    event_key       VARCHAR(128) NOT NULL,
    payload         JSONB        NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
    attempts        INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    dispatched_at   TIMESTAMPTZ,
    CONSTRAINT ck_event_outbox_status CHECK (status IN ('PENDING', 'SENT', 'DEAD_LETTER'))
);

CREATE INDEX IF NOT EXISTS ix_event_outbox_dispatchable
    ON event_outbox (status, next_attempt_at);

-- ---------------------------------------------------------------------------
-- Admin tasks. rules.yaml#EXIT-04 (dead letter) and #EXIT-05 (stalled exit).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_tasks (
    id          UUID PRIMARY KEY,
    task_type   VARCHAR(64) NOT NULL,
    workflow_id VARCHAR(20),
    status      VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_admin_tasks_type CHECK (
        task_type IN ('EXIT_STALLED', 'NOTIFICATION_DEAD_LETTER')),
    CONSTRAINT ck_admin_tasks_status CHECK (status IN ('OPEN', 'CLOSED'))
);

CREATE INDEX IF NOT EXISTS ix_admin_tasks_open
    ON admin_tasks (task_type, status);

COMMIT;
