# Tenant Exit Workflow

Backend module implementing the tenant exit workflow from the Meridian PropTech SRS v1.2:
**T13** (ten-step exit flow), **O15** (third-party inspection workflow), **O16** (deposit
settlement and NOC issuance) and **BR-1** (exit workflow contract lock).

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL · Alembic.

---

## Quick start

```bash
pip install -e ".[dev]"
createdb exit_workflow
cp .env.example .env          # set DATABASE_URL and JWT_SECRET
alembic upgrade head
uvicorn app.main:app --reload # docs at http://localhost:8000/docs
```

Tests need a real PostgreSQL database — the module relies on partial unique indexes, check
constraints, native enums, sequences and `SELECT … FOR UPDATE`, none of which a SQLite
stand-in would exercise:

```bash
createdb exit_workflow_test
TEST_DATABASE_URL=postgresql+asyncpg://user@localhost:5432/exit_workflow_test pytest
```

---

## The workflow

T13 specifies ten steps. They map onto the state machine in `app/domain/states.py`, refined
with the inspection sequence from Appendix B and the settlement sequence from O16:

| T13 step | State reached | Actor |
|---|---|---|
| 1–3, 5 · exit section, move-out date, reason, workflow ID | `DRAFT` / `DOCUMENTS_PENDING` | Tenant |
| 4 · document upload | *(no state change)* | Tenant |
| 6 · owner notification | `PENDING_OWNER_APPROVAL` → `OWNER_APPROVED` | Tenant → Owner |
| 7 · inspection scheduling | `INSPECTION_SCHEDULING` → `INSPECTION_SCHEDULED` → `INSPECTION_COMPLETED` | Agency ↔ parties |
| 8 · damage review | `DAMAGE_REVIEW` | Agency → parties |
| 9 · deposit refund | `PENDING_SETTLEMENT` → `SETTLED` | Owner (and tenant, if a balance is due) |
| 10 · NOC download, completion | `NOC_ISSUED` → `COMPLETED` | System → Tenant |

Off-ramps: `CANCELLED` (any time before money moves) and `REJECTED` (owner declines).

`app/domain/states.py` holds the only transition table, and every state change in the module
funnels through `WorkflowService.transition`, which validates against it and appends a row to
the append-only `exit_workflow_transitions` history. A transition that is not in the table is
a `409 invalid_state_transition` carrying the legal alternatives.

---

## Decisions the SRS left open

The SRS is precise about the happy path and silent on several edges. Each of these is a
deliberate choice, marked so a reviewer can find and change it.

**Damages exceeding the deposit.** O16 says "deposit minus damage" but never says what
happens when damage is larger. The refund floors at zero and the excess becomes a
`balance_due` owed by the tenant. A settlement has two legs — the owner's refund and the
tenant's balance — and closes only when both are satisfied; a leg worth zero is satisfied on
approval. **NOC issuance is gated on that close**, so an unpaid shortfall blocks the
certificate. This keeps BR-1's lock meaningful instead of letting an under-recovered owner
absorb the loss silently. See `app/domain/settlement.py`.

**"Until the exit workflow is marked COMPLETE" (BR-1).** Read literally, a cancelled or
rejected workflow would block the property forever, since neither is `COMPLETE`. The lock
therefore keys on *in-flight* workflows (`ACTIVE_STATES`): a workflow that never resulted in
an exit releases the property and the tenant. See `app/services/contract_service.py`.

**Completion on download.** T13 orders step 10 as "NOC download → workflow completion", so
the first download completes the workflow. A tenant who never downloads would otherwise
strand the property, so `POST /exit-workflows/{id}/complete` can close it explicitly too.
Completion is idempotent; later downloads only bump the counters.

**Notice period.** Not specified. `MIN_NOTICE_DAYS` / `MAX_NOTICE_DAYS` are configurable and
default to 0 / 365. Dates are validated against the Dubai calendar day, not UTC.

**Required documents.** T13 mandates an upload step but not which documents.
`REQUIRED_DOCUMENT_KINDS` is configurable and defaults to `EMIRATES_ID`. A workflow starts in
`DOCUMENTS_PENDING` when documents are required and `submit` refuses until they are present,
naming what is missing.

**Storage.** §15 lists MongoDB *and* PostgreSQL for T13. This module is PostgreSQL-only as
specified in the brief; `app/models/catalog.py` holds a relational projection of the parties,
properties and contracts mastered elsewhere, so the database can enforce referential
integrity on settlements and audit rows.

---

## How correctness is enforced

**Money is never a float.** Everything is integer *fils* (1 AED = 100 fils) in a `BIGINT`.
`app/money.py` refuses sub-fils precision rather than rounding it away silently.

**The ledger is checked by the database, not just the code.** `deposit_settlements` carries
constraints requiring both sides non-negative, at most one side non-zero, and
`refund − balance_due = deposit − deductions`. A settlement that does not balance cannot be
written, whatever the application layer believes.

**BR-1 is enforced three times, deliberately.** A read-only `GET /contracts/eligibility`
probe so portals can render the warning *before* the user acts; a blocking check on
`POST /contracts`; and partial unique indexes on `exit_workflows` (one active workflow per
contract, one per property) as the backstop that holds when two requests race past the
check. A blocked attempt is audited out-of-band — in its own transaction — because the
request that triggered it is about to roll back, and the attempt is exactly what compliance
needs to see.

**Concurrency.** Every state-changing operation takes a `SELECT … FOR UPDATE` row lock on the
workflow and bumps an optimistic `version` column; a racing writer gets a `409` rather than a
lost update. `is_active` mirrors the state and a check constraint ties the two together, so
the flag the unique indexes depend on cannot drift.

**Payments are idempotent.** `idempotency_key` is globally unique and doubles as the
provider-side key. Replaying "Pay Deposit" returns the original transaction; a *new* key
against an already-settled leg is a `409`, not a second payment. A partial unique index
permits at most one `SUCCEEDED` payment per settlement leg.

**Side effects are transactional.** Domain events and notifications are written to an
`outbox_events` row in the same transaction as the state change. A relay drains it
afterwards, dispatching Kafka events and notifications to their adapters, retrying failures
with an attempt counter. A rolled-back request emits nothing; a committed one cannot lose its
event.

---

## The Exit NOC

Generated automatically the moment the settlement closes — O16's "auto-generated upon
payment" — rendered to a real PDF, SHA-256 hashed, and stored immutably alongside a snapshot
of every figure printed on it.

`app/domain/pdf.py` is a self-contained PDF 1.4 writer: A4 pages, the two standard Helvetica
faces (built into every reader, so nothing needs embedding), word wrap driven by real Adobe
font metrics, automatic pagination and a correct cross-reference table. No external library,
no system binary, and byte-for-byte deterministic — which is what makes the stored hash a
tamper check rather than decoration. The download response carries it in `X-Content-SHA256`.

---

## Security

Tenants and owners authenticate with HS256 bearer tokens minted by the platform identity
service; inspection agencies use an `X-Agency-Key` API key, stored only as a SHA-256 digest.

Token verification is implemented directly against `hmac`/`hashlib` (`app/security.py`) rather
than via a JWT library: there is no cryptographic dependency to keep patched, and the accepted
algorithm set is a hard-coded allowlist, so `"alg": "none"` and algorithm-confusion attacks are
structurally impossible. Tests cover forged signatures, wrong secrets, expiry, issuer and
audience mismatch, and the `alg: none` bypass.

Authorisation is per-workflow, not merely per-role: a tenant reaches only their own workflows,
an owner only theirs, and an agency only workflows it holds a live assignment for. Settlement
legs additionally check the payer — the owner cannot pay the tenant's balance.

---

## Layout

```
app/
  domain/          pure logic — no DB, no I/O, no framework
    states.py        the transition table (single source of truth)
    settlement.py    O16 arithmetic
    pdf.py           dependency-free PDF writer
  models/          SQLAlchemy models, constraints and indexes
  services/        transaction scripts; all state changes funnel through WorkflowService
  ports/           outbound protocols + adapters (events, notifications, payments, outbox)
  api/             routes, dependencies, auth wiring
  schemas/         Pydantic wire contract
alembic/versions/  migrations (round-trips cleanly, enum types included)
tests/             67 tests against real PostgreSQL
```

## Endpoints

| | |
|---|---|
| `POST /exit-workflows` | Initiate (steps 1–3, 5) |
| `GET /exit-workflows` · `GET /exit-workflows/{id}` | List / fetch, with ten-step progress |
| `POST /exit-workflows/{id}/documents` | Attach a document (step 4) |
| `POST /exit-workflows/{id}/submit` | Submit to the owner (step 6) |
| `POST /exit-workflows/{id}/approve` · `/reject` · `/cancel` | Owner decision, cancellation |
| `POST /exit-workflows/{id}/inspection` · `/inspection/select-slot` · `/inspection/reinspect` | Scheduling (step 7) |
| `GET /exit-workflows/{id}/damage-report` | Damage review (step 8) |
| `GET /exit-workflows/{id}/settlement` · `/settlement/approve` · `/settlement/dispute` · `/settlement/pay` | Settlement (step 9) |
| `GET /exit-workflows/{id}/noc` · `/noc/download` | NOC metadata and PDF (step 10) |
| `POST /exit-workflows/{id}/complete` | Explicit completion |
| `GET /agency/assignments` · `/assignments/{id}/slots` · `/complete` · `/report` | Agency-facing (O15) |
| `GET /contracts/eligibility` · `POST /contracts` | BR-1 probe and enforcement |
| `GET /health` · `/health/ready` | Liveness / readiness |

Errors share one envelope with a stable machine-readable `code`, so clients branch on
failures without matching message strings:

```json
{"error": {"code": "invalid_state_transition",
           "message": "cannot transition exit workflow from DOCUMENTS_PENDING to OWNER_APPROVED",
           "details": {"current_state": "DOCUMENTS_PENDING",
                       "allowed_next_states": ["CANCELLED", "PENDING_OWNER_APPROVAL"]},
           "request_id": "…"}}
```

## Performance

§5.1 targets p95 < 200 ms. Measured in-process (excludes network), 200 requests after warm-up:

| Endpoint | p50 | p95 |
|---|---|---|
| `GET /exit-workflows/{id}` | 14.7 ms | 15.9 ms |
| `GET /contracts/eligibility` | 14.9 ms | 16.0 ms |
| `GET /health` | 1.4 ms | 1.7 ms |

Reads use `selectin` eager loading (no N+1); BR-1 lock checks are single-table index lookups
against denormalised `property_id` / `tenant_id` columns. Every response carries
`X-Response-Time-ms` and `X-Request-ID`, and logs are structured JSON correlated by request ID.

## Operational notes

- **Kafka** is optional and off by default. `KAFKA_ENABLED=true` additionally requires
  `aiokafka`; with it off, the relay still runs and logs, so local and production exercise the
  same delivery path.
- **Notifications** default to a logging adapter. Swapping in SES/SendGrid means implementing
  `NotificationPort` and changing one line of wiring.
- **Payments** run through `PaymentGateway`. The default `InternalLedgerGateway` moves the
  deposit between internally held balances and succeeds synchronously, which is right for a
  deposit in the platform's own escrow; a PSP adapter drops in without changes above that line.
- **Audit rows** carry an explicit `retain_until` (default seven years, SRS A3) so a retention
  job can prune by predicate.
- **NOC PDFs** are stored inline. A deployment preferring object storage can move `pdf_bytes`
  out and keep the hash without touching the rest of the module.
