# Meridian — Tenant Exit Workflow Module

Production backend module for the tenant exit workflow: **initiation → inspection →
deposit settlement → NOC issuance → completion**.

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL · Alembic.

Implements, from *Meridian PropTech SRS v1.2*:

| SRS | Requirement | Where |
|---|---|---|
| **T13** | Exit Initiation Workflow (10 steps) | `app/services/exit_workflow.py`, `app/domain/state_machine.py` |
| **O15** | 3rd-Party Inspection Workflow | `app/services/inspection.py` |
| **O16** | Return Deposit & Settlement, Exit NOC | `app/services/settlement.py`, `app/services/noc.py` |
| **BR-1** | Exit Workflow Lock on new contracts | `app/services/contract_guard.py`, partial unique indexes on `exit_workflow` |
| **A3** | Audit trails, 7-year retention | `app/services/audit.py`, `exit_workflow_audit_log` |
| **§7** | Kafka events | transactional outbox + `app/workers/outbox_dispatcher.py` |
| **§5.1** | API p95 < 200 ms | keyset pagination, partial indexes, no network I/O inside transactions |

---

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

createdb meridian
export EXITFLOW_DATABASE_URL="postgresql+asyncpg://localhost:5432/meridian"
alembic upgrade head

uvicorn app.main:app --reload
```

OpenAPI docs at <http://localhost:8000/docs>.

Run the tests (they provision their own database):

```bash
export EXITFLOW_TEST_DATABASE_URL="postgresql+asyncpg://localhost:5432/meridian_test"
pytest
```

Tests that need PostgreSQL skip cleanly if no database is reachable; the domain,
policy, money and PDF tests always run.

---

## The ten steps, and where each one lives

SRS T13 defines the flow as: *Exit section → move-out date → reason entry → document
upload → Workflow ID generation → owner notification → inspection scheduling → damage
review → deposit refund (deposit minus damage) → digital NOC download → workflow
completion.*

| # | Step | Endpoint | State after |
|---|---|---|---|
| 1–3 | Initiate; move-out date; reason | `POST /exit-workflows`, `PATCH /exit-workflows/{id}` | `DRAFT` |
| 4 | Document upload | `POST /exit-workflows/{id}/documents` | `DRAFT` |
| 5–6 | Workflow ID generated; owner notified | `POST /exit-workflows/{id}/submit` | `SUBMITTED` |
| — | Owner approves; agency emailed (O15) | `POST /exit-workflows/{id}/approve` | `OWNER_APPROVED` |
| 7 | Agency offers dates; a date is chosen | `.../inspection/slots`, `.../inspection/schedule` | `INSPECTION_SLOTS_PROPOSED` → `INSPECTION_SCHEDULED` |
| — | Inspection occurs; report uploaded (O16) | `.../inspection/report` | `INSPECTION_COMPLETED` |
| 8 | Damage review (adjust / dispute) | `.../damage-items/{id}` … | `DAMAGE_REVIEW` |
| 9 | Deposit refund (deposit minus damage) | `.../settlement/finalise`, `.../settlement/pay` | `SETTLEMENT_PENDING` → `SETTLEMENT_PROCESSING` → `SETTLEMENT_COMPLETED` |
| 10 | Digital NOC download | `.../noc/content` | `NOC_ISSUED` |
| 11 | Workflow completion | `.../complete` | `COMPLETED` |

`REJECTED`, `CANCELLED` and `EXPIRED` are terminal off-ramps. Like `COMPLETED`, they
release the BR-1 lock — an abandoned request must not block a property forever.

The full transition table, including which role may trigger each action, is
`app/domain/state_machine.py`. Nothing outside `WorkflowEngine.transition` assigns
`ExitWorkflow.state`.

---

## Architecture

```
app/
  api/          HTTP: routers, deps, error envelope, middleware, idempotency
  services/     use cases; the only place state changes
  domain/       enums, state machine, business policies, events  (pure, no I/O)
  models/       SQLAlchemy mappings
  repositories/ data access
  ports/        outbound interfaces (storage, payments, notifications, events, PDF)
  adapters/     concrete implementations of the ports
  workers/      outbox dispatcher, reconciler
```

`domain/` has no imports from SQLAlchemy or FastAPI: the notice period, the dispute
window, "deposit minus damage" and BR-1 are plain functions over plain values, and are
unit-tested as such.

Services depend on `ports/`, never on an SDK, so the whole ten-step flow — payment and
NOC rendering included — runs in a test without a network.

### Correctness properties worth knowing about

**One writer per workflow.** Every command loads the aggregate with `SELECT … FOR
UPDATE`, and the row also carries a SQLAlchemy `version_id_col`. Two concurrent
approvals, or an approve racing a cancel, cannot both observe the same pre-state.

**BR-1 is enforced in the database.** Partial unique indexes
(`uq_exit_workflow_active_property`, `…_active_contract`, `WHERE state NOT IN (terminal)`)
mean two API workers cannot create two active exits for a property, whatever the
application layer does. `ContractGuard` provides both the advisory read (with the
SRS-mandated warning text) and a raising `assert_allowed` for the contract-insert path.

**No network I/O inside a transaction.** Notifications go through
`UnitOfWork.after_commit`. The payment provider is called *after* the payout transaction
commits, and its outcome is written in its own transaction.

**Payouts cannot double-pay.** `POST …/settlement/pay` requires an `Idempotency-Key`,
and the settlement carries a stable provider idempotency key bound to the settlement,
not to the click. A settlement stranded in `PROCESSING` — because the process died
between commit and dispatch — is re-driven by the reconciler with that same key.

**Events cannot be lost or phantom-emitted.** Domain events are written to
`exit_workflow_outbox` in the same transaction as the state change; the dispatcher
relays them with `FOR UPDATE SKIP LOCKED`, exponential backoff and a dead-letter status.
Delivery is at-least-once, keyed by `event_id`; per-workflow ordering is preserved by
partitioning on the workflow id.

**Money is never a float.** Amounts arrive as JSON *strings*, are parsed to `Decimal`,
stored as `NUMERIC(14,2)`, and the settlement identity is a database CHECK constraint:
`net_refund = GREATEST(deposit - deductions, 0)`.

**Documents are verified on the way out.** Every download re-hashes the blob and refuses
to serve it if the SHA-256 no longer matches what was recorded.

---

## Deposit settlement

`net_refund = max(deposit − deductions, 0)` and
`tenant_liability = max(deductions − deposit, 0)`.

The SRS describes only the happy path ("deposit minus damage"). Where deductions exceed
the deposit, the refund floors at zero and the excess is recorded as
`tenant_liability_amount` — surfaced on the settlement, the workflow and the NOC, and
explicitly *not* discharged by the certificate. Pursuing it is out of scope for this
module.

Deduction lines come from two places: chargeable damage items from the current
inspection round, and manual lines the owner adds at finalisation (unpaid rent,
utilities, admin fees) that an inspection cannot know about. Both are **frozen** onto the
settlement, so a NOC issued today still reconciles in seven years even if the underlying
damage items are later corrected.

An unresolved tenant dispute blocks finalisation. The owner may reduce or waive a charge
but never raise it above the agency's assessment — that requires a re-inspection.

## The NOC

Rendered by a dependency-free PDF writer (`app/adapters/pdf.py`) using only the base-14
fonts, so the bytes we checksum today render identically in seven years without a
rendering stack in the trust path. Output is deterministic for a given set of facts.

Each certificate carries a transcribable verification code (`K7QW-3M2X-9RTP`, no
`I`/`O`/`0`/`1`) checkable at an unauthenticated, rate-limited endpoint that confirms
validity without disclosing the parties, the address or any settlement figure.

Swap the renderer by implementing `app.ports.noc_renderer.NocRenderer`.

---

## Configuration

All settings are `EXITFLOW_`-prefixed environment variables; see `app/core/config.py`
and `.env.example`. Production refuses to boot with placeholder secrets.

The knobs the SRS leaves open are configuration, not code:

| Setting | Default | Note |
|---|---|---|
| `MIN_NOTICE_DAYS` | 30 | Not specified in the SRS; customary Dubai notice period |
| `MAX_MOVE_OUT_HORIZON_DAYS` | 365 | |
| `MIN_DOCUMENTS_FOR_SUBMISSION` | 1 | T13 mandates "document upload" without naming a set |
| `REQUIRED_DOCUMENT_TYPES` | *(none)* | Set to enforce a specific set |
| `DISPUTE_WINDOW_DAYS` | 5 | Not in the SRS; damage review needs a bound |
| `AUTO_COMPLETE_AFTER_NOC_DAYS` | 7 | Stops an inattentive party holding a property un-lettable |
| `DRAFT_EXPIRY_DAYS` | 30 | |
| `AUDIT_RETENTION_YEARS` | 7 | SRS A3 |

Backends are selected by configuration: storage `local` \| `s3`, payments `null` \|
`http`, events `outbox-only` \| `kafka` \| `log`, notifications `log` \| `http`.

---

## Deviations from the SRS, and why

* **Documents live in PostgreSQL, not MongoDB.** §7 assigns documents to MongoDB, but
  this module was specified as PostgreSQL + SQLAlchemy. Only document *metadata* is in
  PostgreSQL — where it keeps referential integrity and the audit trail transactional —
  and the bytes sit behind `ports.storage.DocumentStorage` (filesystem or S3). Pointing
  that port at GridFS is a one-class change.

* **Workflow ID timing.** T13 sequences ID generation at step 5 (after document upload);
  Appendix B O15 describes it as generated when the owner approves. We follow T13: the
  reference (`EXW-2026-000123`) is allocated at submission, and owner approval is what
  triggers the agency email. Drafts have a `NULL` reference.

* **States the SRS does not name.** `SETTLEMENT_PROCESSING` exists because real payouts
  are asynchronous. `REJECTED`/`CANCELLED`/`EXPIRED` exist because a workflow that can
  only move forward would hold a property hostage indefinitely.

* **Owner rejection.** The SRS describes approval but not its refusal. Rejection is
  terminal and requires a reason; the tenant can start a fresh request.

* **Notification delivery is best-effort.** The durable trigger for anything that must
  not be lost is the outbox event stream. Wire a consumer to it rather than relying on
  the inline notifier.

## Things this module deliberately does not do

* **No malware scanning.** Uploads are content-type- and magic-byte-checked and stored
  with `scan_status = SKIPPED` — honestly recording that they were *not* scanned here.
  Wire the platform's scanning pipeline to flip the column.
* **No payment instrument storage.** The settlement holds a tokenised
  `payout_account_ref` and a last-4, never an IBAN.
* **No contract creation.** BR-1 is enforced *for* the Property service, which must call
  `ContractGuard.assert_allowed` inside its insert transaction.
* **Rate limiting is a backstop only.** The in-process limiter on NOC verification
  guards a single replica; the real control belongs at the gateway.

---

## Operations

```bash
alembic upgrade head          # migrations
python -m app.workers.run     # outbox dispatcher + reconciler as a separate deployment
```

By default the workers run in-process (`EXITFLOW_ENABLE_BACKGROUND_WORKERS=true`); set
it to `false` when running them separately. Multiple replicas of either are safe.

The reconciler sweeps: abandoned drafts, workflows whose NOC nobody collected, payouts
stranded in `PROCESSING`, and expired idempotency keys.

`GET /health` is liveness; `GET /ready` checks the database.

Every response carries `X-Request-ID`, which appears on every log line and in every
audit row for that request.
