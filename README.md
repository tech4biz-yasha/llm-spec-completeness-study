# Specification Completeness and LLM-Generated Backend Code
### Evidence pack for a controlled six-run study

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX)
**Paper:** *Specification Completeness, Not Model Capability, Determines the Reliability of LLM-Generated Backend Code* — Khandelwal, 2026

---

## What this is

The same AI model (Claude Opus 5, via Claude Code) was asked to build the same backend module six times: same prompt, same 30-minute budget, fresh session every time. The only variable was the input.

- **Group A (runs A1–A3)** received the relevant sections of a real software requirements specification, verbatim.
- **Group B (runs B1–B3)** received a "build kit": the same requirements with every open question answered in machine-readable form, and the genuinely undecided questions explicitly marked as blocked.

This repository contains everything produced and everything needed to re-run or re-score the study: both input packages, the complete generated codebase of all six runs, the fixed test yardstick, the scoring checklist, and hash-sealed archives.

## Headline results

| Run | Input | Cost (USD) | Finished in budget | Tests passing at stop | Invented business decisions |
|-----|-------|-----------|--------------------|----------------------|------------------------------|
| A1  | SRS   | ~14.00*   | Yes                | 67                   | 7  |
| A2  | SRS   | 13.40     | No                 | 0                    | 8+ |
| A3  | SRS   | 15.14     | No                 | 0                    | 8+ |
| B1  | Kit   | 13.15     | Yes                | 81                   | 0  |
| B2  | Kit   | 8.55      | Yes (9 min early)  | 57                   | 0  |
| B3  | Kit   | 12.92     | Yes                | 104                  | 0  |

\* A1 cost estimated from the on-screen token counter; all others from the tool's usage panel.

Across eight pre-registered decision points (timezone, deposit shortfall, idempotency, and so on), the three Group A runs gave two or more **different** behaviours on six of the eight, given identical input. The three Group B runs converged on identical behaviour on all eight, and all three independently **refused** the one deliberately unresolved question with an explicit `SpecUnresolved` error, a 501 response, and a logged finding — rather than inventing an answer.

## Repository layout

```
inputs/
  group-A-srs/        The verbatim SRS extract Group A received (one file)
  group-B-kit/        The eight-file build kit Group B received
runs/
  A1/ A2/ A3/         Complete generated codebase of each SRS run, untouched
  B1/ B2/ B3/         Complete generated codebase of each kit run, untouched
scoring/
  invented-decisions-checklist.md   The fixed 12-point checklist, written before any run
  test_exit_workflow.py             The acceptance-test yardstick
  results-template.md               The measurement sheet
archives/
  run-*.zip, inputs.zip             Sealed copies of the above
  SHA256-MANIFEST.json              Hash of every archive; binds this repo to the scored versions
```

The `runs/` directories are exactly what the agent produced at the stop line, minus virtual environments and caches. Nothing was edited except anonymisation (see below). Each Group B run contains a `blockers.md` written by the agent itself, listing the specification questions it refused to resolve.

## Reproducing the study

1. Install [Claude Code](https://docs.claude.com/en/docs/claude-code) and authenticate.
2. Create an empty directory containing **only** `inputs/group-A-srs/exit-workflow-srs.md` (for an A run) or the contents of `inputs/group-B-kit/` (for a B run).
3. Start a fresh session and paste the prompt from the paper, Section 3.3 (also in `scoring/results-template.md`).
4. Approve tool permissions freely. If the agent asks a specification question: for an A run answer in one line taking its recommended option; for a B run reply only "follow the kit". Log every such exchange.
5. Stop at 30 minutes of working time. Record cost and tokens from `/cost`.
6. Score the output with `scoring/invented-decisions-checklist.md` and run `scoring/test_exit_workflow.py` against it.

Total cost of the original six runs was roughly $77.

## Anonymisation

The source specification belongs to a real commercial project. All identifying names were replaced with the fictional "Meridian PropTech" across inputs and outputs before publication (52 files touched, name substitution only). No other content was altered; the SHA-256 manifest was generated after anonymisation and matches the scored artifacts.

## Known honest limitations

Three runs per condition demonstrates existence, not precise rates. The kit was authored with knowledge of this specification's gaps — that is the method under test. The invented-decisions scoring was assisted by the same model family that ran the study, verified by the operator; the full artifacts are published here precisely so anyone can re-score them. See the paper's Section 5 for the complete list.

## Citing

If you use this dataset, cite the paper and this archive:

> Khandelwal, Y. (2026). *Specification Completeness, Not Model Capability, Determines the Reliability of LLM-Generated Backend Code: A Controlled Six-Run Study.* Evidence pack: doi:10.5281/zenodo.XXXXXXX

A machine-readable citation is in `CITATION.cff`; GitHub's "Cite this repository" button uses it.

## License

Code (generated runs, test yardstick): MIT. Documents and data (inputs, checklist, manifest): CC BY 4.0. See `LICENSE`.
