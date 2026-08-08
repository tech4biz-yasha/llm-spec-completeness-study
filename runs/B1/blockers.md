# Known open items. These are deliberate. Do not resolve them in code.
- R8: damage > deposit behaviour undecided. Raise SpecUnresolved. (EXIT-07)

---

# Questions the kit does not answer

Added while building the exit workflow module, per AGENTS.md ("If the spec does not
answer a question, STOP. Add the question to blockers.md and mark that branch BLOCKED
in code by raising SpecUnresolved"). Each entry names who has to decide, what is
blocked, and exactly what the code does in the meantime.

Nothing below is guessed. Where an item has a defensible reading, the reading and the
line it comes from are stated so a reviewer can overturn it in one edit.

## Part 1 — BLOCKED in code (raises SpecUnresolved, HTTP 501)

### R8 — damage exceeding the deposit (already in the register above)
**Raised by:** `services/settlement.py` on `confirmed_damage > security_deposit`.
**Effect:** 501 `SPEC_UNRESOLVED_R8`. No refund, no payment row, no NOC; the workflow
holds at DAMAGE_CONFIRMED. Covered by `test_x003`.

### R1 — is the BR-1 exit lock scoped to the role or to the identity?
**Spec:** risks.md#R1, rules.yaml#EXIT-03, edges.yaml#X-006.
**Raised by:** `services/guards.assert_tenant_contractable`.

edges.yaml#X-006 specifies the *property*-scoped guard only, and that one is
implemented: a new contract on a property mid-exit gets 409
`EXIT_WORKFLOW_INCOMPLETE`. The *identity*-scoped half of BR-1 — whether a dual-role
user exiting a tenancy is also barred from contracting as an owner — has two live
readings and is not resolvable from the kit.

**Decision needed:** role scope or identity scope.

### B-1 — the exit reason reference list has never been published
**Spec:** rules.yaml#EXIT-02 ("a reason from the reference list"), api.yaml
`REASON_INVALID`, risks.md open items ("Reference data dictionary, specifically
**exit reasons** (blocks the ExitWorkflow enum)").
**Raised by:** `domain/reasons.validate_reason` when no list is configured.

The validation *mechanism* is built and tested; the vocabulary is absent. A plausible
list written here would silently accept or reject real tenants' submissions on invented
grounds, so the module ships with none and refuses to judge a reason until the
dictionary exists. Deployments populate `EXIT_EXIT_REASON_CODES` once it does.

**Decision needed:** publish the exit reason reference data.

### B-3 — workflow ID counter exhaustion and reset semantics
**Spec:** rules.yaml#EXIT-02 (`EX-YYYYMMDD-NNNNN`, "sequence from PostgreSQL").
**Raised by:** `domain/ids.format_workflow_id` above 99 999.

The format fixes NNNNN at five digits. The kit does not say whether the counter resets
per day or runs globally, nor what happens at 100 000. A single monotonic sequence is
used (the reading under which the ID is unique without a daily reset job); overflow
refuses to emit a malformed or colliding ID.

**Decision needed:** daily reset or global counter, and the behaviour at exhaustion.

## Part 2 — behaviour specified, error code missing

api.yaml fixes the HTTP status for these branches but defines no code string, and
AGENTS.md forbids inventing one. The response carries `"code": null` and the blocker ID
in a `blocker` field.

### B-4 — no error code for "contract is not ACTIVE"
**Spec:** algorithm.md step 1 ("Assert status == ACTIVE, else 422"), rules.yaml#EXIT-01.
api.yaml lists only `MOVE_OUT_DATE_IN_PAST | REASON_INVALID | DOCUMENTS_REQUIRED` for
422 on this endpoint. **Decision needed:** add a code, or fold this into the authz
(403) path — api.yaml's authz line reads "tenant, own active contract only", which
could be read either way.

### B-6 — no error code, and no recovery policy, for a FAILED refund
**Spec:** algorithm.md step 11 ("PENDING or FAILED -> hold, never proceed"),
edges.yaml#X-004 covers PENDING only. api.yaml offers `PAYMENT_PENDING` for 409 and
nothing for a failure. The workflow is held at DAMAGE_CONFIRMED with the failed payment
recorded. **Decision needed:** a code, plus whether a failed refund retries, is
re-submitted manually, or escalates to admin.

### B-9 — no error codes for inspection-report validation
**Spec:** api.yaml records only `200: ok` for /inspection-report; rules.yaml#EXIT-06
says the assessment is entered "with photos". A report with no photos, or with an
unusable `damage_amount`, is refused with 422 and no code. **Decision needed:** are
photos mandatory, and what code do these failures carry?

## Part 3 — conflicts and gaps resolved by a stated reading

These would have stopped the build, so each names the line it follows. Confirm or
overturn them.

### B-2 — a STALLED workflow has no way out
**Spec:** states.yaml#exit_workflow (STALLED has no outgoing transition, and
`STALLED -> COMPLETE` is explicitly forbidden), rules.yaml#EXIT-05 ("It does not
auto-cancel").
**Reading:** STALLED is terminal, because the transition table defines no exit from it.
Any attempt to schedule, confirm or settle a stalled workflow gets 409 `WRONG_STATE`.
**Decision needed:** what an admin does with a stalled exit — resume to
INSPECTION_SCHEDULED, cancel, or something else. Whatever it is, it needs a transition
in states.yaml.

### B-5 — the owner dispute has no state, transition, or endpoint
**Spec:** rules.yaml#EXIT-06 and algorithm.md step 8 both grant the owner one dispute
"routed to admin review". states.yaml has no DISPUTED state and no edge for it; api.yaml
has no path.
**Reading:** nothing is built. There is no dispute endpoint, no dispute counter, and no
admin-review state.
**Decision needed:** the dispute state, its transitions in and out, the endpoint, and
what "admin review" resolves to.

### B-7 — the workflow document cannot be in MongoDB and still be atomic
**Spec:** AGENTS.md ("Motor/MongoDB for the workflow document") versus
rules.yaml#EXIT-03 ("property.exitLock is set true IN THE SAME TRANSACTION as the
workflow insert") and algorithm.md step 4, which puts the workflow insert, the lock and
the audit row in one transaction. The lock and the audit row are PostgreSQL rows.
**Reading:** the workflow document is in PostgreSQL, so the transaction the rule demands
actually exists. The repository layer is a seam; a Mongo-backed store can replace it if
the atomicity requirement is relaxed.
**Decision needed:** keep single-store atomicity, or accept a two-phase/compensating
write and rewrite EXIT-03.

### B-8 — the 201 body reports a state the workflow is not in
**Spec:** api.yaml (`201: {workflow_id, status: INITIATED}`) versus algorithm.md step 4
(the initiation transaction moves the workflow INITIATED -> DOCS_SUBMITTED).
**Reading:** the response follows api.yaml verbatim, because that is the contract
clients are generated from. The persisted status is DOCS_SUBMITTED.
**Decision needed:** change the response to the persisted state, or change the algorithm
so the transition happens later.

### B-10 — the NOC's content is not specified
**Spec:** rules.yaml#EXIT-09 fixes the format (PDF), the location (UAE region bucket),
immutability and the workflow link — not the wording, layout, language, signature or
seal.
**Reading:** the PDF renders only facts already held on the workflow (references,
move-out date, deposit, damages, refund, payment, issue timestamp in Asia/Dubai). It
asserts nothing the kit has not decided, and it is rendered with WinAnsi base fonts, so
an Arabic document is refused rather than mangled.
**Decision needed:** legal sign-off on the wording; whether an Arabic version is
required (that needs an embedded font); whether a signature or seal is required.

### B-11 — three endpoints have no response schema
**Spec:** api.yaml records `200: ok` for /schedule-inspection, /inspection-report and
/confirm-damage.
**Reading:** each returns `{workflow_id, status}` — what the call did, nothing more.
**Decision needed:** confirm the body, or fix it as empty.

## Also noted, not blocking

* **`tests/acceptance/` did not exist.** AGENTS.md's definition of done refers to tests
  there as "written and failing". The kit contained no test directory, so the suite was
  written from edges.yaml — one test per edge case, named as its `test` field.
* **states.yaml says `inspector`, api.yaml says `inspection_agency`** for the party that
  submits the report. Treated as one role (`ActorRole.INSPECTION_AGENCY`).
* **api.yaml is not valid YAML.** `{contract_id, move_out_date, reason, documents[]}` is
  a YAML-like shorthand that no parser accepts. The kit is the contract and was not
  edited; `domain/spec.api_error_codes` extracts the code list from the raw text instead.
* **No payment gateway is named.** rules.yaml#EXIT-08 requires "the gateway confirms
  SUCCEEDED" without saying whose; risks.md records gateway selection as open. The
  module defines the port and fails closed with no adapter bound.
