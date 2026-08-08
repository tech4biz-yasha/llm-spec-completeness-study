# Tenant exit workflow module

Initiation through completion, including deposit settlement and NOC issuance. The spec kit
in this folder is the contract: `AGENTS.md`, `rules.yaml`, `states.yaml`, `edges.yaml`,
`api.yaml`, `algorithm.md`, `risks.md`, `blockers.md`.

## Running it

```bash
pip install -e ".[dev]"
export EXIT_DATABASE_URL=postgresql+psycopg2:///exit_workflow
alembic upgrade head
pytest
```

The test suite needs a real PostgreSQL (row locks, a sequence, unique constraints and
append-only triggers are all load-bearing). Point `EXIT_TEST_DATABASE_URL` at a database
whose `public` schema the test user may drop; it defaults to `exit_workflow_b_test`.

Wire the app yourself — there is no module-level `app` singleton, because two ports have
no default the kit permits:

```python
from exit_workflow.app import build_app
from exit_workflow.ports.reference import StaticExitReasonReference

app = build_app(
    reasons=StaticExitReasonReference(load_approved_exit_reasons()),  # blockers.md#B-2
    gateway=your_payment_gateway,                                    # blockers.md#B-12
    storage=your_uae_bucket,                                         # rules.yaml#EXIT-09
    publisher=KafkaEventPublisher({"bootstrap.servers": "..."}),     # rules.yaml#EXIT-04
    principal_resolver=your_auth_adapter,                            # blockers.md#B-7
)
```

`build_app` refuses to start without an exit-reason reference list, and refuses a NOC
bucket outside a UAE region.

## Scheduled jobs

Three loops belong on a scheduler; none of them are HTTP endpoints:

| Job | Rule | Call |
| --- | --- | --- |
| Stall sweep | EXIT-05 | `service.run_stall_sweep()` |
| Notification retries | EXIT-04 | `service.dispatch_pending_notifications()` |
| Orphaned-notify recovery | EXIT-04 | `service.recover_unnotified()` |

## Layout

```
src/exit_workflow/
  spec/            byte-identical copies of the kit, loaded at runtime
  domain/states.py state machine compiled from states.yaml, forbidden list included
  db/              SQLAlchemy models + session/transaction boundaries
  ports/           gateway, storage, NOC renderer, event bus, exit-reason reference
  adapters/        Kafka, S3, in-memory doubles, dependency-free PDF renderer
  services/        initiation, inspection, settlement, NOC, stall sweep, outbox, exit lock
  api/             the five api.yaml routes, error handlers, principal resolution
migrations/        schema, workflow-ID sequence, append-only triggers
tests/acceptance/  test_x001..test_x007 from edges.yaml, plus rule-level coverage
```

## What is deliberately absent

- **No exit reason values.** risks.md carries the reference dictionary as open; the module
  validates against injected data instead. blockers.md#B-2
- **No dispute endpoint.** rules.yaml#EXIT-06 mentions one; states.yaml and api.yaml define
  none. `dispute_damage` raises `SpecUnresolved`. blockers.md#B-1
- **No damage-exceeds-deposit handling.** risks.md#R8. `settle` raises `SpecUnresolved`
  and answers 501 SPEC_UNRESOLVED_R8; the workflow holds at DAMAGE_CONFIRMED.
- **No invented error codes.** Every `code` comes from `api.yaml#_error_codes`, checked at
  import. Where api.yaml defines no code (403, 404, non-R8 blocked branches) the field is
  null rather than fabricated.

Read `blockers.md` before extending this module: it lists every question the kit does not
answer, which of them stop the code, and which reading the code takes where the kit
contradicts itself.
