-- Tables owned by OTHER modules (property, contract). They are declared here only
-- so this module can be provisioned and tested standalone. In an environment where
-- the property/contract modules already own these relations, skip this file — the
-- exit workflow module reads them and writes exactly one column: properties.exit_lock
-- (rules.yaml#EXIT-03).

CREATE TABLE IF NOT EXISTS properties (
    id          uuid PRIMARY KEY,
    owner_id    uuid NOT NULL,
    -- rules.yaml#EXIT-03: set true inside the initiation transaction, released
    -- only by workflow COMPLETE. Blocks new contracts on the property (BR-1).
    exit_lock   boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS contracts (
    id                      uuid PRIMARY KEY,
    property_id             uuid NOT NULL REFERENCES properties (id),
    tenant_id               uuid NOT NULL,
    status                  text NOT NULL,
    -- AGENTS.md: money is stored in minor units (fils), never float.
    security_deposit_minor  bigint NOT NULL CHECK (security_deposit_minor >= 0),
    currency                char(3) NOT NULL DEFAULT 'AED'
);

CREATE INDEX IF NOT EXISTS ix_contracts_property ON contracts (property_id);
CREATE INDEX IF NOT EXISTS ix_contracts_tenant ON contracts (tenant_id);
