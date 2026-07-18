# Contributing

Thanks for your interest. Before you change anything, understand what this
project *is*: **`gromacs-bench` is an argument with measurements attached, not a
benchmark suite.** Several design choices look like they could be simplified and
cannot — each protects a specific claim. **Read [`THESIS.md`](THESIS.md) first**,
then [`PLAN.md`](PLAN.md) and [`README.md`](README.md).

## Ground rules

1. **Read `THESIS.md` before touching the harness.** The failure-modes list at
   the end names decisions that are locally reasonable and quietly destroy the
   argument. If your change touches any of them, say why in the PR.
2. **Nothing here has been run against live infrastructure.** Syntax is
   validated; behavior is not. Do not claim a code path works end-to-end unless
   you have actually exercised it.
3. **This is authorized-cost tooling.** Anything that spawns instances spends
   real money. Never add a code path that launches without a TTL and idle
   timeout, and never widen concurrency caps without saying so.

## The invariants — do not break these

These are load-bearing. A PR that violates one will be asked to justify it
against the thesis before anything else.

- **`DRY_RUN=1 python run_matrix.py` after every edit to `run_matrix.py`.**
  Expected: **90 cells / 90 rows**. A past edit orphaned the success path inside
  an `except` block — valid Python, silently unreachable — and only the dry run
  caught it.
- **Every cell runs ≥3 timed replicates and reports a 95% CI.** ns/day is noisy;
  one run is an anecdote. `results.csv` carries `ns_day_ci95` / `ns_day_rel_ci`.
  When two arms' CIs overlap, the ranking between them is **not** a finding.
- **`ns/day` and `$/ns` only — never `$/result`.** `$/result` needs a
  convergence criterion declared in advance; emitting a half-defined one here
  reproduces the exact error the thesis is about. Convergence stays downstream
  (`PLAN-cost-per-result.md`).
- **`wait_s = acquire_s + provision_s`, symmetric across providers.** Do not
  collapse the split — the on-prem-vs-cloud scheduling comparison lives in it.
  Cloud is not exempt from `infeasible:capacity`.
- **Infeasible cells are rows, not gaps.** They emit via `blank_row()` with a
  typed reason. `skip:` is only for genuinely uninteresting combos.
- **Timing runs are on-demand.** Spot interruption pollutes timing; spot price
  enters only through the `ns_per_dollar_spot` column.
- **Publish the arms that lose.** HMR, a SIMD build flag, or local hardware may
  beat every cloud decision. Those rows are what make the rest credible.

## The `# SPORE:` markers

Every `# SPORE:` comment in `spore.py` and `providers.py` is **guessed** CLI
syntax for the spore.host tools (`truffle` / `spawn` / `lagotto`). Before any
live run, confirm each against the installed tool, fix the call, and remove the
marker. Do not remove a marker you have not verified.

Similarly, `providers._epoch()` assumes `sacct` emits `%Y-%m-%dT%H:%M:%S` local
time — verify per site or every on-prem wait number is wrong.

## Placeholders and stubs

- `CHANGE-ME` tokens in `matrix.yaml` are pre-deployment blockers by design.
  Never commit a run that resolves them with real secrets — they are meant to
  stay as placeholders in the repo.

## Workflow

1. Fork and branch from `main`.
2. Make the change. If it touches `run_matrix.py`, paste the `DRY_RUN=1` output
   (must show 90 rows) in the PR. Run `bash -n mdrun_wrapper.sh` and
   `python -m py_compile *.py` for syntax.
3. Open a PR that references the issue it addresses. The issue tracker mirrors
   the doc taxonomy: **milestones = harness phases**, and labels carry
   `tier-N` (demos D1–D20), `capability-gap` (G1–G9), tool, and flags
   (`load-bearing`, `blocker`, `deferred`).
4. If your change diverges from FASRC Cannon behavior or from a thesis
   invariant, note it explicitly.

## Capability gaps

If a spore.host tool lacks a measurement the harness needs, don't work around it
silently — file it (or extend the matching `G#` issue). `GAPS.md` explains why
each gap degrades the comparison. G1, G2, and G6 are load-bearing; G6 blocks D9.

## License

By contributing you agree your contributions are licensed under the
[Apache License 2.0](LICENSE).
