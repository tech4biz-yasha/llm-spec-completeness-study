# Exit Workflow Module — Agent Instructions

You are building ONE module: the tenant exit workflow. The spec files in this folder are
the contract. Every line you write must trace to an item in them.

## The rule that matters most
If the spec does not answer a question, STOP. Add the question to blockers.md and mark
that branch BLOCKED in code by raising SpecUnresolved. Do NOT invent an answer. Gaps
listed in risks.md are known and deliberate; none of them are yours to resolve.

## Stack
Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x on PostgreSQL for payments and audit,
Motor/MongoDB for the workflow document. Kafka producer for events.

## Conventions
- Money is Decimal, minor units in storage, AED, 2 decimal places. Never float.
- Timestamps stored UTC. Business logic timezone: Asia/Dubai (decision D-001).
- Cite the rule ID in a comment at every branch: # rules.yaml#EXIT-03
- Error codes come from api.yaml verbatim. Never invent one.
- Every state transition validated against states.yaml, forbidden list included.
  A forbidden transition raises, never silently no-ops.
- Audit rows are append-only. Enforced by DB trigger, not application code.

## Definition of done
- tests in tests/acceptance/ pass (they are written and failing)
- zero TODO / FIXME / assumed markers
- every function implementing a rule cites its ID
