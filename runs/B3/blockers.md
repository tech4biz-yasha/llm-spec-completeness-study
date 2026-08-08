# Known open items. These are deliberate. Do not resolve them in code.
- R8: damage > deposit behaviour undecided. Raise SpecUnresolved. (EXIT-07)

---

# Questions raised while building the exit workflow module

Added per AGENTS.md ("If the spec does not answer a question, STOP. Add the question to
blockers.md and mark that branch BLOCKED in code by raising SpecUnresolved").

Two groups. **Blocked branches** raise `SpecUnresolved` and refuse to run — nothing was
guessed. **Recorded readings** are places where the kit contradicts itself or leaves an
integration detail open; the code takes the reading stated here, and each is cited at the
line that implements it.

## Blocked branches — code raises SpecUnresolved

### B-1 — Owner dispute has no state, no transition and no endpoint (HIGH)
**Spec:** rules.yaml#EXIT-06, states.yaml#exit_workflow, api.yaml
**Blocked in:** `ExitWorkflowService.dispute_damage`

EXIT-06 says "Owner may dispute once; a dispute routes to admin review." Nothing else in
the kit supports it:
- states.yaml has no disputed/under-review state, and no transition into or out of one.
- api.yaml declares no dispute endpoint and no error code for a dispute.
- No field records that the one permitted dispute has been used.
- Nothing says what state the workflow holds during admin review, or where it returns to
  when review concludes — INSPECTION_DONE for a re-inspection, or DAMAGE_CONFIRMED with
  an admin-set amount.

**Decision needed:** (a) the state and the transitions, (b) the endpoint and its authz,
(c) where admin review returns the workflow to and who may set the final damage figure.
Until then the service raises and no route is exposed.

### B-4 — Workflow ID sequence has no reset rule and overruns its field (MEDIUM)
**Spec:** rules.yaml#EXIT-02
**Blocked in:** `services.ids.next_workflow_id`

EXIT-02 fixes the format at `EX-YYYYMMDD-NNNNN` with the counter coming from a PostgreSQL
sequence. A plain sequence passes 99999 at the 100 000th workflow and no longer fits
NNNNN; a per-day sequence would fit forever but is not what "sequence from PostgreSQL"
says, and the kit never states which.

**Decision needed:** does NNNNN reset daily (making the date prefix part of the key), or
is it global with a wider field? The code uses a global sequence and stops rather than
emit an ID that violates the stated format.

## Recorded readings — the kit is contradictory or silent on an integration detail

### B-2 — Exit reason reference list does not exist
**Spec:** rules.yaml#EXIT-02, risks.md (Appendix A carry-over)

EXIT-02 requires "a reason from the reference list" and api.yaml gives REASON_INVALID, but
risks.md carries "Reference data dictionary, specifically **exit reasons** (blocks the
ExitWorkflow enum)" as an open item. AGENTS.md says risks.md gaps are not ours to resolve,
so the module defines **no** reason values and has no `ExitReason` enum. The list arrives
through the `ExitReasonReference` port; `build_app` refuses to start without one, and an
empty list is rejected rather than silently turning every initiation into REASON_INVALID.

**Still needed:** the approved list. Nothing else about initiation is blocked by it.

### B-3 — When does the workflow reach OWNER_NOTIFIED?
**Spec:** rules.yaml#EXIT-04, edges.yaml#X-002, states.yaml

X-002 says the workflow "stays DOCS_SUBMITTED->OWNER_NOTIFIED path intact" when dispatch
fails, which can be read two ways: the workflow advances regardless and only the event
retries, or the transition waits for a successful delivery.

**Reading taken:** the workflow advances once the event is durably queued, and delivery
retries independently. The other reading leaves a dead-lettered workflow stuck at
DOCS_SUBMITTED forever, with no path out — the owner could never schedule an inspection
because of a broker outage. Implemented as a transactional outbox: the event row is
written inside the initiation transaction, dispatch happens strictly after commit, and a
publish failure cannot roll anything back.

**Confirm:** that a dead-lettered notification should leave the workflow advanced (with an
admin alert) rather than held.

### B-5 — Inspection report has no validation rules and no error codes
**Spec:** rules.yaml#EXIT-06, api.yaml#/exit-workflows/{id}/inspection-report

EXIT-06 says the agency enters the assessment "with photos". api.yaml declares only
`200: ok` for the endpoint and its `_error_codes` list contains nothing for a photo-count
or amount problem. So no minimum photo count is imposed and no upper bound is placed on
`damage_amount` — inventing either would need an error code that does not exist. The only
constraint applied is the AGENTS.md money convention (AED, Decimal, 2 dp, non-negative).

**Decision needed:** minimum photos, and the error code to report when it is not met.

### B-6 — api.yaml's 201 status contradicts algorithm.md#4
**Spec:** api.yaml (`201: {workflow_id, status: INITIATED}`), algorithm.md#4

algorithm.md#4 has the initiation transaction insert the workflow and move it
`INITIATED->DOCS_SUBMITTED` before commit, so by the time the 201 is written the workflow
is DOCS_SUBMITTED. api.yaml says the response carries `INITIATED`.

**Reading taken:** the response carries the status the workflow actually holds
(`DOCS_SUBMITTED`). Returning `INITIATED` would tell a polling client something untrue
about a state the workflow has already left.

Related, same file: algorithm.md#1 requires 422 when the contract is not ACTIVE, but
api.yaml lists no code for it — its 422 codes are all about the request payload. The
response uses status 422 (per the algorithm) with code `WRONG_STATE`, the only verbatim
code that fits. **Confirm** or add a code.

### B-7 — No authentication model, and no codes for authz/not-found
**Spec:** api.yaml `authz` lines, risks.md#R3

api.yaml states who may call each endpoint but nothing describes how a caller is
authenticated, and R3 leaves session scope, token revocation and role-toggle semantics
open. The module therefore verifies no tokens: the principal arrives through an injected
resolver, and the shipped default trusts identity headers asserted by the edge gateway.
That is only correct where the gateway is the authentication boundary.

`_error_codes` also has no entry for an authorization failure or an unknown workflow, so
those responses carry `"code": null` with a message rather than an invented code.

**Decision needed:** R3, then the authentication adapter; and codes for 403/404.

### B-8 — NOC document has no template
**Spec:** rules.yaml#EXIT-09

EXIT-09 fixes the format (PDF), the location (UAE bucket), the immutability and the link
to the workflow, but says nothing about wording, letterhead, signatory, language (Arabic
is likely required for a UAE clearance document) or whether a RERA/Ejari reference must
appear. The shipped renderer lays out only facts already on the workflow and is behind the
`NocRenderer` port so the approved template can replace it without touching the workflow.

**Decision needed:** the approved template, and whether Arabic is mandatory.

### B-9 — The inspector role has two names
**Spec:** states.yaml (`actor: inspector`) vs api.yaml (`authz: inspection_agency`)

Taken as the same party. `Actor.normalize` maps `inspection_agency` onto `inspector` so
the states.yaml transition table stays the single source of truth. **Confirm** they are
one role and pick a spelling.

### B-10 — Notification backoff base is unspecified
**Spec:** rules.yaml#EXIT-04

"Exponential backoff, 5 attempts" fixes the attempt count and the shape but not the first
delay or the cap. Both are deployment settings (`EXIT_NOTIFICATION_BACKOFF_BASE_SECONDS`,
default 60 s; `EXIT_NOTIFICATION_BACKOFF_CAP_SECONDS`, default 3600 s). The attempt count
is not configurable away from 5 in any deployment that follows the rule.

### B-11 — "Admin task" and "admin alert" have no definition
**Spec:** rules.yaml#EXIT-05, #EXIT-04

Both rules require an admin task/alert but the kit defines no task schema, no assignee, no
priority, no SLA and no channel for the alert. Tasks are written to a local `admin_tasks`
table carrying the type, the workflow and the facts that produced them, one open task per
(workflow, type). **Decision needed:** the real admin task model and where alerts go.

### B-12 — A terminally FAILED refund has no recovery path
**Spec:** algorithm.md#11, rules.yaml#EXIT-08, edges.yaml#X-004

"PENDING or FAILED -> hold, never proceed" is implemented exactly: the workflow holds at
REFUND_PROCESSED and settle answers 409 PAYMENT_PENDING. But the idempotency key is the
workflow ID, so the same key can never produce a second disbursement — a refund that fails
terminally (bad IBAN, closed account) can never be retried, and the workflow can never
reach COMPLETE or release the property's exit lock.

**Decision needed:** how a failed refund is re-attempted — a new key with an audited
reason, or an admin-driven manual settlement path. Nothing is implemented for it.

### B-13 — STALLED has no way out
**Spec:** rules.yaml#EXIT-05, states.yaml#exit_workflow

EXIT-05 says a stalled workflow "does not auto-cancel" and states.yaml forbids
`STALLED -> COMPLETE`. It also lists no transition *out of* STALLED at all, so every
onward call is refused with 409 WRONG_STATE and the property stays exit-locked
indefinitely. That is what the state machine says, and it is what the code does.

**Decision needed:** what an admin may do with a stalled workflow — resume it to
INSPECTION_SCHEDULED, cancel it and release the lock, or something else.
