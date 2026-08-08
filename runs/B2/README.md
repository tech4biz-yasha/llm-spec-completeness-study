# Tenant exit workflow module

Initiation through completion, including deposit settlement and NOC issuance.
Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x (async) on PostgreSQL, Kafka
producer for events.

The spec files in this folder are the contract. Every branch traces to a rule id,
cited in a comment at the branch. Open questions are in `blockers.md` and raise
`SpecUnresolved` at the point of use — none of them is answered in code.

## Layout

```
exit_workflow/
  domain/states.py       state machine, parsed from spec/states.yaml at runtime
  domain/rules.py        pure rules: refund, ids, dates, the 30-day window
  services/              one module per stage of algorithm.md
  db/models.py           SQLAlchemy models mirroring the migrations
  migrations/*.sql       schema of record, incl. the append-only triggers
  adapters/              Kafka, object store, NOC renderer, reference data
  ports.py               everything the module needs from outside, as Protocols
  api/                   the five endpoints in api.yaml, and nothing else
  workers.py             notification retry loop and stall sweep
tests/unit/              domain, money, state machine — no database
tests/acceptance/        edges.yaml cases and the end-to-end flow, on PostgreSQL
```

`domain/states.py` parses `spec/states.yaml` (a byte copy of the kit's file)
rather than transcribing it, so the machine cannot drift from the spec. Every
status change goes through `TransitionService.apply`, which validates against
that machine — forbidden list first — and writes the audit row. There is no other
writer of `exit_workflows.status`.

## Running

```bash
pip install -e '.[test]'

# schema
psql "$DATABASE_URL" -f exit_workflow/migrations/0000_external_tables.sql   # dev/test only
psql "$DATABASE_URL" -f exit_workflow/migrations/0001_exit_workflow.sql

# tests
python -m pytest tests/unit
EXIT_WORKFLOW_TEST_DATABASE_URL=postgresql+asyncpg://localhost/exit_workflow_test \
  python -m pytest tests/acceptance
```

Acceptance tests run against a real PostgreSQL and rebuild the schema per test:
the partial unique index, the append-only trigger, `SELECT ... FOR UPDATE` and
`ON CONFLICT` are the guarantees under test, and a substitute engine would test
something other than what ships. They skip if no server is reachable.

The application is assembled by `build_container(...)`, which takes the four
outbound ports (event publisher, payment gateway, NOC renderer, object store) and
the exit reason reference source, then `create_app(container)`. `workers.py` has
the two loops the spec requires: notification retries (EXIT-04) and the stall
sweep (EXIT-05).

## How the spec maps to the code

| Rule | Where |
|---|---|
| EXIT-01 one active workflow per contract | `services/initiation.py`, partial unique index `uq_exit_workflow_open_per_contract` |
| EXIT-02 date/reason/documents, id format | `domain/rules.py`, `api/schemas.py` |
| EXIT-03 exitLock in the same transaction | `services/initiation.py`, `services/contract_guard.py` |
| EXIT-04 notification after commit, 5 retries, dead-letter | `services/notification.py` (transactional outbox) |
| EXIT-05 30-day stall + admin task | `services/stall.py`, `domain/rules.py` |
| EXIT-06 agency report, owner confirmation | `services/inspection.py` |
| EXIT-07 refund arithmetic, R8 hold | `domain/rules.py::refund_minor` |
| EXIT-08 DEPOSIT_REFUND, idempotency key, SUCCEEDED only | `services/settlement.py` |
| EXIT-09 NOC PDF, UAE bucket, immutable, COMPLETE + unlock in one transaction | `services/noc.py`, `adapters/`, DB triggers |
| EXIT-10 append-only audit | `services/transitions.py`, trigger in `migrations/0001` |

## Decisions the specification makes that the code follows literally

- **T13 order, not O16.** Refund first, then NOC (EXIT-08; risks.md#R10 resolved
  in favour of T13). `any -> NOC_ISSUED without REFUND_PROCESSED` is enforced by
  the state machine and re-checked against the payment status before issuance.
- **Owner confirmation is mandatory.** `INSPECTION_DONE -> REFUND_PROCESSED` is
  forbidden (EXIT-06); settlement from any state but DAMAGE_CONFIRMED is 409.
- **Asia/Dubai calendar.** `move_out_date` is a date, compared against the Dubai
  day (D-001, X-007). Nothing in the module calls `date.today()`.
- **Money.** `Decimal` everywhere, integer minor units in storage, half-up at
  2 dp, AED. `to_minor()` rejects a float outright.
- **Audit immutability is the database's job**, not the application's: UPDATE,
  DELETE and TRUNCATE on `exit_workflow_audit` all raise (EXIT-10). The same
  guard covers `noc_documents` (EXIT-09).

## What is deliberately not built

`blockers.md` has the full list with the question each one needs answered. The
short version: no exit reason list (B-001), no dispute path (B-002), no way out
of STALLED (B-003), no payment gateway adapter (B-011), no NOC template (B-010),
and damage-above-deposit raises `SpecUnresolved("R8")` (risks.md#R8) rather than
capping, writing off or recording a debt.
