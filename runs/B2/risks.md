# Risk register — Meridian PropTech

Twelve findings that must be closed before or during the build. Each names the exact
decision a person has to make, why it matters, and what is blocked until it is made.

These are not defects in the SRS. They are the questions a document written for humans
never has to answer, and a code generator cannot answer for you.

---

## R1 — BR-1 and BR-4 collide on dual-role users (HIGH)
**Blocks:** T-018, T-019 · **Spec:** rules.yaml#BR-1, #BR-4

BR-4 puts a dual-role user under one Master Customer ID. BR-1 says a Tenant with an
incomplete exit workflow cannot enter any new contract.

If that same person is also an Owner, are they blocked from *listing and contracting their
own property* because of an unrelated tenancy they are exiting?

**Decision needed:** is the BR-1 lock scoped to the role, or to the identity?
**Why it matters:** the wrong reading either freezes legitimate owner revenue or leaves the
absconding-tenant hole BR-1 exists to close.

---

## R2 — Verified badge trigger is contradicted (HIGH)
**Blocks:** T-024, T-025 · **Spec:** rules.yaml#BR-2, states.yaml#photography_job

BR-2 and F10 both state that photographer upload is the **sole** trigger, with no admin
toggle. A9 requires "AI-driven quality check followed by admin review **before
publishing**."

Both cannot be true. **Decision needed:** does the badge appear on upload, or after admin
review? If after, BR-2 must be rewritten.

---

## R3 — "Single session enforcement" has no defined scope (HIGH)
**Blocks:** T-004, T-005 · **Spec:** rules.yaml#BR-4, edges.yaml#E-006, #E-015

F1 mandates single session per account. The system has a mobile app, a web portal, an
admin portal, and a role toggle.

**Decisions needed:** (a) is a session per user, per user-device, or per user-role?
(b) when a new login displaces an old one, how is a 7-day refresh token revoked?
(c) does toggling role reissue the JWT?

Session design cannot start until all three are answered.

---

## R4 — Photography job has no assignment SLA or failure path (MEDIUM)
**Spec:** rules.yaml#BR-5, edges.yaml#E-013

The owner pays first, then the request goes to a shared email inbox. There is no
assignment logic, no SLA, no escalation, and no refund rule if nobody picks it up.

**Decision needed:** what happens on day 3 with no photographer assigned?
The owner has paid the platform for a service with no defined outcome.

---

## R5 — Contracts pending counter-signature never expire (HIGH)
**Blocks:** T-020 · **Spec:** states.yaml#contract, edges.yaml#E-012

O11 pushes a contract to the tenant for counter-signature. Nothing defines an expiry, an
owner cancellation path, or escalation.

A contract sitting in PENDING_TENANT_SIGNATURE holds the property lock indefinitely,
which means an unresponsive tenant can freeze an owner's unit.

**Decision needed:** signature window duration and what happens at expiry.

---

## R6 — Rent frequency inheritance on renewal (MEDIUM)
**Blocks:** T-031, T-032 · **Spec:** rules.yaml#BR-7

O9 stores rent frequency per property. O25 renews contracts. If the owner changed the
property setting mid-tenancy, does the renewal inherit the old contract's frequency or
adopt the new property setting?

Payment schedules, reminder timing and DDA mandates all follow from this.

---

## R7 — Reminder timing has no timezone and no suppression rule (MEDIUM)
**Spec:** rules.yaml#BR-9, edges.yaml#E-004

T6 specifies a reminder at 07:00 on the due date. **In which timezone is not stated.**
Asia/Dubai is assumed throughout this kit and is not confirmed.

Separately: if rent is already paid, are the remaining reminders suppressed? Almost
certainly yes, but T6 does not say so, and a reminder sent after payment is the single
most common complaint in rent platforms.

---

## R8 — Damage exceeding the deposit is undefined (HIGH)
**Blocks:** T-021 · **Spec:** rules.yaml#BR-8, edges.yaml#E-011

T13 defines the refund as "deposit minus damage". Nothing covers damage greater than the
deposit.

Three viable readings, each producing different entities and a different NOC rule:
cap at zero and write off; cap at zero and raise a recoverable debt; or block exit until
settled. **The client must pick one.**

---

## R9 — Owner IBAN override is an open fraud path (CRITICAL)
**Blocks:** T-028 · **Spec:** rules.yaml#BR-10, api.yaml#/payments/setup-recurring

T9 auto-populates owner bank details "with manual override". It does not say who may
override, or whether the substituted IBAN is validated against the owner of record.

As written, a party who can set the IBAN can redirect rent. **Treat as security-critical.**
Recommendation: ship without the override field until a validation rule exists.

---

## R10 — Exit workflow step order conflicts between BRDs (MEDIUM)
**Spec:** states.yaml#exit_workflow

T13 orders the final steps as refund, then NOC. O16 says "generate exit NOC after
inspection", placing NOC before refund.

This kit follows T13. If O16 is correct, a tenant receives clearance while money is still
outstanding.

---

## R11 — BNPL instalment failure semantics are undefined (HIGH)
**Spec:** states.yaml#payment, edges.yaml#E-010

T8 is a MUST requirement allowing rent via Tabby or Tamara. A BNPL provider pays the
platform up front and collects from the tenant over time.

If a later instalment fails: is the rent still paid as far as the owner is concerned?
Who absorbs the loss? Does the contract enter arrears? None of this is specified for a
MUST requirement.

---

## R12 — No business calendar for UAE weekends and holidays (MEDIUM)
**Spec:** edges.yaml#E-016

Rent due dates, DDA mandates and reminder scheduling all need a business calendar. The UAE
federal weekend moved to Saturday–Sunday in 2022, but Friday–Saturday persists in parts of
the private sector, and public holidays are announced with short notice.

**Decision needed:** which calendar source, and what happens when a due date lands on a
non-business day — advance, defer, or leave as-is?

---

## Open items also flagged in the SRS itself

Appendix A already lists these as pending. They are carried here so they are not lost:

- Reference data dictionary, specifically **exit reasons** (blocks the ExitWorkflow enum)
- Finalised payment modes — O18 records a pending decision on Central Bank and Al Etihad
  gateways, while F6 is a MUST requirement. A MUST cannot depend on an open decision.
- Wireframes and UI/UX specifications
- Kick-off call transcripts
