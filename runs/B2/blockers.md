# Known open items. These are deliberate. Do not resolve them in code.
- R8: damage > deposit behaviour undecided. Raise SpecUnresolved. (EXIT-07)

---

# Questions raised while building the exit workflow module

Each item below is a question the kit does not answer. None is resolved in code.
Where a branch cannot proceed without an answer it raises `SpecUnresolved` with
the id given here; where the module had to ship something to be usable at all,
the "Interim" line says exactly what it does and why that is not a decision.

## B-001 — Exit reason reference list does not exist
**Blocks:** initiation (rules.yaml#EXIT-02, api.yaml 422 REASON_INVALID) ·
**Also flagged in:** risks.md, Appendix A ("Reference data dictionary,
specifically exit reasons")

What are the permitted exit reasons, and which system owns the list?

**In code:** `validate_reason()` validates against an injected reference list and
hard-codes nothing. With no list configured it raises `SpecUnresolved("B-001")`,
so a deployment cannot silently accept arbitrary reasons. The default adapter
(`UndefinedExitReasonReference`) returns no list.

## B-002 — Owner damage dispute has no state, transition or endpoint
**Blocks:** rules.yaml#EXIT-06 ("Owner may dispute once; a dispute routes to
admin review")

states.yaml declares no dispute or admin-review state, the only transition out of
INSPECTION_DONE is to DAMAGE_CONFIRMED, and api.yaml exposes no dispute endpoint.

**Decisions needed:** which state does a disputed workflow hold in; who resolves
it; does an admin decision overwrite the agency's damage figure; what does "once"
mean operationally (is a second dispute a 409, and against which code)?

**In code:** `InspectionService.dispute_damage()` raises `SpecUnresolved("B-002")`
and no endpoint is exposed.

## B-003 — STALLED is a dead end
**Blocks:** rules.yaml#EXIT-05

states.yaml declares two transitions *into* STALLED and none out of it, and
forbids STALLED -> COMPLETE. EXIT-05 also says the workflow "does not
auto-cancel". So a stalled exit permanently holds `property.exitLock` (EXIT-03),
which permanently blocks new contracts on that property (BR-1).

**Decision needed:** what an admin may do to a STALLED workflow — resume it to
INSPECTION_SCHEDULED, cancel it and release the lock, or something else.

**In code:** the stall sweep moves the workflow and opens an admin task. There is
no code path out of STALLED, because states.yaml declares none.

## B-004 — No error code for initiation on a non-ACTIVE contract
**Spec:** algorithm.md step 1 ("Assert status == ACTIVE, else 422"), api.yaml

api.yaml lists exactly three 422 codes: MOVE_OUT_DATE_IN_PAST, REASON_INVALID,
DOCUMENTS_REQUIRED. None covers contract status.

**Interim:** returns 422 with `code: null` and a message. No code is invented.

## B-005 — No error code, and no disposition, for a FAILED refund
**Spec:** algorithm.md step 11 ("PENDING or FAILED -> hold, never proceed"),
api.yaml (`409: WRONG_STATE | PAYMENT_PENDING`)

A gateway decline is not "pending". The spec says hold, but not what the caller
is told, whether a retry is permitted, or how many times.

**Interim:** the payment row records status FAILED and the failure reason, the
workflow holds at DAMAGE_CONFIRMED, and the response is 409 PAYMENT_PENDING with
the true payment status in `details`. PAYMENT_PENDING is the only code api.yaml
offers for "the refund is not settled"; a distinct code would have to be invented.

## B-006 — No error code for authorisation failures
**Spec:** api.yaml `authz` lines, `_error_codes`

Every endpoint names an authorised role, but no code exists for a caller who is
not that role, and no status is specified.

**Interim:** 403 with `code: null`. Authentication itself is out of this module
(risks.md#R3 leaves session design undecided); the principal is taken from the
platform edge.

## B-007 — Initiation response status contradicts the initiation algorithm
**Spec:** api.yaml (`201: {workflow_id, status: INITIATED}`) vs algorithm.md
step 4 ("insert workflow (INITIATED->DOCS_SUBMITTED)")

The workflow is already DOCS_SUBMITTED when the transaction that creates it
commits, so no client can ever observe INITIATED.

**Interim:** the response returns the persisted status (DOCS_SUBMITTED).
Returning a status the row does not hold would break any client that polls it.

## B-008 — Workflow id sequence has no reset rule and no overflow rule
**Spec:** rules.yaml#EXIT-02 ("EX-YYYYMMDD-NNNNN, sequence from PostgreSQL")

Does NNNNN reset each day, or is it one global sequence? What happens after
99999 in whichever namespace applies?

**Interim:** one global PostgreSQL sequence. Past 99999 the id cannot be formatted
without either truncating (duplicate ids) or widening (violating the stated
format), so `format_workflow_id()` raises `SpecUnresolved("B-008")` rather than
mint a bad id.

## B-009 — No recovery path when the owner notification dead-letters
**Spec:** rules.yaml#EXIT-04, edges.yaml#X-002

After 5 failed attempts the event dead-letters and an admin is alerted. The
workflow is then stuck in DOCS_SUBMITTED: states.yaml offers no transition from
DOCS_SUBMITTED other than OWNER_NOTIFIED, and no DOCS_SUBMITTED -> STALLED.

**Decision needed:** may an admin force the notification (re-queue) or force the
transition, and under whose actor identity?

**In code:** the workflow is left in DOCS_SUBMITTED with an OPEN
NOTIFICATION_DEAD_LETTER admin task. Nothing forces a transition.

## B-010 — No NOC document specification
**Spec:** rules.yaml#EXIT-09 ("NOC is a PDF")

Format, letterhead, legal wording, language(s), signatory, and whether the
document needs a verifiable reference or QR code are all unspecified.

**Interim:** the renderer is a port. The shipped implementation prints only facts
the spec establishes (parties, tenancy ids, move-out date, deposit arithmetic,
refund payment reference, issue date in Asia/Dubai). No legal text is invented.
A real template replaces the adapter without touching the workflow.

## B-011 — Payment gateway is not chosen
**Also flagged in:** risks.md, Appendix A ("Finalised payment modes — O18 records
a pending decision")

**In code:** `PaymentGateway` is a port with no concrete adapter in this module.
Writing one would mean inventing a third party's contract. The DEPOSIT_REFUND
payment record, the idempotency key and the SUCCEEDED-only progression are all
implemented and gateway-agnostic.

## B-012 — No rule ties an inspection agency to a workflow
**Spec:** api.yaml (`authz: inspection_agency`), rules.yaml#EXIT-06

Nothing states which agency may file the report for a given workflow, or how an
agency is assigned. As written, the role alone is the authorisation.

**In code:** any principal holding the `inspection_agency` role may file the
report. That is the spec as written, not a chosen policy.

## B-013 — Inspection photo requirements are unstated
**Spec:** rules.yaml#EXIT-06 ("entered by the inspection agency with photos")

Minimum count, accepted formats, size limits and retention are not given.

**Interim:** at least one photo reference is required (the plain reading of "with
photos"); the payload is stored as supplied. No format or retention policy is
imposed.

## B-014 — Storage divergence from AGENTS.md
**Spec:** AGENTS.md ("SQLAlchemy 2.x on PostgreSQL for payments and audit,
Motor/MongoDB for the workflow document")

The build instruction for this module was PostgreSQL throughout. The workflow
document therefore lives in PostgreSQL alongside payments and audit, not MongoDB.

**Consequence, and why it was not fought:** rules.yaml#EXIT-03 and #EXIT-09 both
require the workflow row, the audit row and `property.exitLock` to change "IN ONE
TRANSACTION". Split across PostgreSQL and MongoDB that is a distributed
transaction and cannot be honoured literally without an outbox/saga and a window
of inconsistency. On one database it is a single commit. If MongoDB is
reinstated, EXIT-03 and EXIT-09 need a decision on how atomicity is preserved.

## B-015 — The acceptance tests referred to by AGENTS.md do not exist
**Spec:** AGENTS.md, Definition of done ("tests in tests/acceptance/ pass (they
are written and failing)")

No `tests/` directory was present in the kit.

**In code:** `tests/acceptance/` was written from edges.yaml, one test per case
using the ids edges.yaml assigns (`test_x001` … `test_x007`), plus the flow and
invariant tests. If the intended suite exists elsewhere, it should replace these.
