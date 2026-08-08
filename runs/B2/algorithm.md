# Exit workflow — pseudo-algorithm (compiled from decision sheet, lint-passed)

1. Tenant opens exit. Load contract. Assert status == ACTIVE, else 422. (EXIT-01)
2. Assert no existing workflow for contract, else 409 with existing ID. (EXIT-01, X-001)
3. Validate move_out_date >= today in Asia/Dubai, reason in reference list,
   documents >= 1. (EXIT-02, X-007)
4. IN ONE TRANSACTION: insert workflow (INITIATED->DOCS_SUBMITTED), set
   property.exitLock = true, write audit row. (EXIT-03)
5. AFTER COMMIT: emit owner notification event. Failure never rolls back;
   retry 5x backoff then dead-letter. (EXIT-04, X-002)
6. Owner schedules inspection. If 30 days pass beyond move_out_date first,
   system moves workflow to STALLED and opens admin task. (EXIT-05)
7. Agency uploads damage_amount + photos -> INSPECTION_DONE.
8. Owner confirms -> DAMAGE_CONFIRMED. Owner may dispute once -> admin review. (EXIT-06)
9. BRANCH on confirmed_damage vs security_deposit:
   - confirmed_damage <= deposit -> step 10
   - confirmed_damage >  deposit -> raise SpecUnresolved("R8"). STOP. (EXIT-07, X-003)
10. refund = deposit - confirmed_damage, Decimal, half-up 2dp. Create payment
    type DEPOSIT_REFUND, idempotency_key = workflow_id. (EXIT-07, X-005)
11. Await gateway SUCCEEDED. PENDING or FAILED -> hold, never proceed. (EXIT-08, X-004)
12. Generate NOC PDF, store UAE bucket, immutable, link to workflow. (EXIT-09)
13. IN ONE TRANSACTION: status = COMPLETE, release property.exitLock, audit row. (EXIT-09)
