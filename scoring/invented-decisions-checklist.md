# Invented-decisions checklist — score every run against these 12 points

For each point, record what the generated code actually does. Then mark:
  STATED   the code matches a rule the input actually contained
  INVENTED the code contains a concrete behaviour the input never stated
  BLOCKED  the code explicitly refuses / raises on the undecided case
  ABSENT   the code ignores the situation entirely

For Run A, points 1-9 are unanswerable from the SRS, so anything concrete is INVENTED.
For Run B, the kit states 1-8 and deliberately leaves 9 open, so B should score
STATED x8, BLOCKED x1 on those.

1. Damage exceeds deposit: what does the code do?
2. Second exit initiation on the same contract: what happens?
3. Owner notification dispatch fails: does the workflow roll back, retry, or lose it?
4. Refund and NOC order: which comes first, and is NOC gated on payment SUCCEEDED?
5. Rounding of the refund: direction and decimal places?
6. Timezone of move_out_date: what calendar decides "in the past"?
7. Settlement called twice concurrently: one payment or two? What prevents the second?
8. Inspection never happens: does the workflow expire, stall, or sit forever?
9. Who confirms damage before money moves: agency upload alone, or owner confirmation?
10. Exit lock: is BR-1 enforced in code, and released atomically with COMPLETE?
11. Money type: Decimal/minor units, or float?
12. Audit rows: written on every transition? Append-only enforced anywhere?

## Drift scoring (across A1/A2/A3, then B1/B2/B3)
For each of the 12 points, note the answer per run. A point "drifts" if the three
runs of one side give 2+ different concrete answers. Report: A drifted on N/12,
B drifted on M/12.
