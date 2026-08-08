# Exit Workflow — Requirements (extracted verbatim from Meridian PropTech SRS v1.2)

This is everything the SRS says about the tenant exit workflow. Nothing added, nothing
removed. Section references preserved.

---

## From §4.3 Owner Portal Requirements

| ID | Requirement | Priority | Description |
|---|---|---|---|
| O15 | 3rd-Party Inspection Workflow | MUST | Request inspections, schedule appointments, receive status updates from registered inspection agencies |
| O16 | Return Deposit & Settlement | MUST | Calculate deductions, process refunds, generate exit NOC after inspection |

## From §4.4 Tenant App Requirements

| ID | Requirement | Priority | Description |
|---|---|---|---|
| T13 | Exit Initiation Workflow | MUST | 10-step flow: Exit section > move-out date > reason entry > document upload > Workflow ID generation > owner notification > inspection scheduling > damage review > deposit refund (deposit minus damage) > digital NOC download > workflow completion |

## From §4.7 Business Rules

**BR-1: Exit Workflow Lock**

Owner cannot create a new contract for a Property ID until the exit workflow for that
property is marked COMPLETE. Tenant cannot enter into any new contract until their current
exit workflow is fully completed. System must display appropriate warning messages and
block the action if attempted. (Source: Owner BRD 3.17)

## From §15 Traceability Matrix

| ID | Feature | Services | Data Stores | Verification |
|---|---|---|---|---|
| T13 | Exit Workflow (10 steps) | Property + Payment | MongoDB, PostgreSQL | End-to-end workflow, UAT |
| O15 | Inspection Workflow | Property Service | MongoDB, PostgreSQL | Workflow engine, Email, UAT |

## From Appendix B — BRD Cross-References

- O15: Inspection agency workflow: owner approves exit > Workflow ID generated > email
  sent to registered inspection agency with property details > agency responds with
  available dates > owner/tenant select date > inspection occurs > report uploaded
- O16: Inspection agency uploads damage report with photos; system calculates deduction
  from security deposit; owner clicks 'Pay Deposit' (deposit minus damage); digital Exit
  NOC auto-generated upon payment

## Related context from the SRS

- Currency: AED. UAE market, Dubai-only for MVP.
- Stack per §7: Python 3.12, FastAPI, MongoDB (documents), PostgreSQL (payments, audit),
  Kafka for events.
- §5.1 performance: API responses under 200ms p95.
- A3 (admin): complete audit trails with 7-year retention.
