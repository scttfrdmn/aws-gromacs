# Working notes for Claude Code

**Read `THESIS.md` first.** Then `README.md`, then `PLAN.md`. This file is only
the things that are easy to get wrong.

`THESIS.md` is not background reading. The harness is an argument with
measurements attached, and several of its design choices look like they could be
simplified but cannot: the wait/runtime split, the symmetric cloud-vs-on-prem
wait decomposition, infeasible cells as rows, on-demand timing runs, and the
refusal to emit `$/result`. Each exists to protect a specific claim. Before
changing any of them, read why they are there.

The core claim, compressed: *the batch-allocation model silently shaped MD
methodology, and the field mistakes those constraints for scientific
requirements.* ns/day is an artifact of how allocations are requested, not a
scientific quantity. The `elasticity` outcome class is the thesis in one enum
value — adaptive sampling is not slow under a fixed allocation, it is
unexpressible.

## Before doing anything
- Nothing here has been run against live infra. Syntax is validated; behavior is not.
- `# SPORE:` markers: CLI **syntax is verified** (truffle/spawn/lagotto, 2026-07-18),
  as are truffle pricing + the lagotto `history` schema. The markers now flag
  **end-to-end behavior** unconfirmed until a paid run (launch/connect/teardown,
  env propagation, lagotto watch→poll→match). Remove a marker only once its flow
  is exercised live. Prefer `use_lagotto: false` until Phase 1 (retry path fully
  verified).
- `providers._epoch()` assumes `sacct` timestamps are `%Y-%m-%dT%H:%M:%S` local.
  Lagotto uses UTC ISO-8601 via `_epoch_utc()` — do not merge the two.
- Verify `_epoch()` per site.

## Invariants — do not break these
- **`DRY_RUN=1 python run_matrix.py` after every edit to `run_matrix.py`.**
  A prior edit orphaned the success path inside an `except` block: valid Python,
  silently unreachable, produced a handful of rows instead of the full sweep.
  Only the dry run caught it. Expected: **90 cells**.
- Timing runs are on-demand. Do not switch them to spot to save money — spot
  interruption pollutes the timing, and spot price already enters via the
  `ns_per_dollar_spot` column.
- Infeasible cells emit rows via `blank_row()`, they are never dropped.
  `skip:` is for genuinely uninteresting combos only.
- `wait_s = acquire_s + provision_s`, and the split is symmetric across
  providers. Do not collapse it — the on-prem comparison lives in that split.
- The harness emits `ns/day` and `$/ns`. It must not emit `$/result`.
- **Every cell is ≥3 timed replicates → mean + 95% CI** (`ns_day_ci95`,
  `ns_day_rel_ci`). ns/day is noisy; one run is not a measurement. `parse_log.py`
  groups logs by `rep<N>` tag, sums slices *within* a replicate, and reports the
  distribution *across* replicates. Do not revert to single-shot ns/day, and do
  not rank two arms whose CIs overlap. This CI is on the performance proxy only
  — it is NOT scientific `$/result` convergence, which stays downstream.

## Things the harness does NOT do
- Generate `.tpr` files, including the HMR variants. Build with `grompp`.
- Build or push the container images.
- Compute `$/result` or convergence. That is downstream, per
  `PLAN-cost-per-result.md`.

## Guardrails
- Every spawn carries TTL + idle timeout. Verify termination after each phase.
- Cap concurrent instances.
- `--list` before `--phase1` before the full sweep. Always.

## Failure modes that look like good decisions
Full list at the end of `THESIS.md`. The ones most likely to come up while
executing:
- Reporting `ns/day` as the headline. It is an intermediate quantity.
- Dropping infeasible rows because they look like missing data. They are the
  strongest cells in the table.
- Merging `acquire_s` and `provision_s` for tidiness. Deletes the
  scheduling-model measurement.
- Running timing on spot to cut cost. Corrupts the timing.
- Omitting arms that lose. Credibility is the only asset this work has.
- Framing output as "cloud wins" rather than "here is the table."

## If a tool lacks a measurement
`GAPS.md` has nine gaps written as issue text. G1 (lagotto capacity-fire
timestamp) and G2 (watch-only mode) are load-bearing; G6 (truffle interruption
hazard) blocks demo D9. File them rather than working around them silently.
