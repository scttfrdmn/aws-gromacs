# gromacs-bench

A harness for measuring GROMACS **$/result** and **time/result** across AWS
instance families, local hardware, and on-prem clusters — provisioned through
[spore.host](https://spore.host) (`truffle`, `spawn`, `lagotto`).

**Read `THESIS.md` first, then `PLAN.md`.** `THESIS.md` explains why the harness
is shaped the way it is — most of its odd choices are load-bearing for an
argument, and a locally-sensible "improvement" will quietly destroy it.
`PLAN.md` is the specification. This file is just the entry point.

---

## Status

Nothing here has been executed. Every file is validated for syntax only. Two
categories of assumption need confirming before a live run:

1. **All `# SPORE:` marked CLI invocations are guessed syntax** (`spore.py`,
   `providers.py`). Confirm against the installed `truffle` / `spawn` /
   `lagotto` before spending money.
2. **`providers._epoch()`** assumes `sacct` emits `%Y-%m-%dT%H:%M:%S` in local
   time. Verify per site or every on-prem wait number is wrong.

See `GAPS.md` for measurements the harness wants that the tools may not expose —
written as issue text, ready to file.

## Layout

```
THESIS.md                why this is shaped this way -- read first
PLAN.md                  the spec: cells, protocol, phasing, guardrails
PLAN-cost-per-result.md  the 20-demo series this harness feeds (D1-D20)
GAPS.md                  capability gaps to file as issues
matrix.yaml              workloads x instances x configs
run_matrix.py            coordinator; writes results/results.csv
providers.py             aws / local / onprem execution + wait measurement
spore.py                 truffle / spawn wrappers
mdrun_wrapper.sh         on-target runner (single / MIG / MPS paths)
parse_log.py             ns/day extraction, summed across slices
build/                   five GROMACS images (Intel, AMD x2, ARM, CUDA)
```

## Execution order

**Phase 0 — prep (nothing runs on a cell until this is done)**
1. Fill `matrix.yaml`: `s3_bucket`, `region`, the five `images:` URIs, and
   `amortized_hr` for any local arms (write the assumption down somewhere).
2. Build and push the five images from `build/`. `mdrun_wrapper.sh` must land at
   `/opt/bench/mdrun_wrapper.sh` inside each.
3. Stage `.tpr` files to `s3://<bucket>/gromacs-bench/tpr/`. Two per workload:
   the base and an HMR variant (`<system>-hmr.tpr`, repartitioned masses, 4 fs
   `dt`). **Nothing in the harness generates these** — build them with `grompp`.
4. Raise service quotas for GPU and G/VT spot if needed.

**Phase 1 — validate**
```bash
DRY_RUN=1 python run_matrix.py --list      # inspect the cross product first
DRY_RUN=1 python run_matrix.py             # full dry sweep, no spend
python run_matrix.py --phase1              # ONE real cell: small / c8g / cpu-base
```
Confirm the instance actually terminated before going further.

**Phase 2 — sweep**
```bash
python run_matrix.py
```

**Phase 3 — findings**
`results/results.csv`, then the decision table in `PLAN-cost-per-result.md`.

**Phase 6 — generational ladder.** Deferred, not optional. See *Deferred axis*
in `PLAN.md`.

## Rules that are load-bearing

- **Run `DRY_RUN=1` after every edit to `run_matrix.py`.** An earlier edit
  orphaned the success path inside an `except` block — valid Python, silently
  unreachable, a handful of rows instead of the full **90**. The dry run caught
  it; nothing else would have.
- **Every instance carries a TTL and idle timeout.** Verify termination after
  each phase. Cap concurrency.
- **Timing runs are on-demand.** Spot interruptions pollute timing; spot price
  is applied only to the `ns_per_dollar_spot` column.
- **This harness emits `ns/day`, not `$/result`.** ns/day is not a result.
  Derive `$/result` downstream once a convergence criterion exists per
  observable. Do not contaminate the benchmark with a half-defined metric.
- **Every cell runs ≥3 timed replicates and reports a 95% CI.** ns/day is noisy;
  one run is an anecdote. `results.csv` carries `ns_day_ci95`/`ns_day_rel_ci`,
  and when two arms' intervals overlap the ranking between them is not a
  finding. (This is a CI on the performance proxy, separate from `$/result`
  convergence.)
- **Infeasible cells are rows, not gaps.** "Could not run it at all" is a
  finding, and the typed reason is the argument.
- **Publish the arms that lose.** HMR or a SIMD build flag may beat every
  hardware decision here; local hardware may beat cloud on small systems. Those
  rows are what make the rest credible.
- **The framing is "here is the table," never "cloud wins."** See the failure
  modes at the end of `THESIS.md` — each is a reasonable-looking decision that
  destroys the argument.

## Dependencies

Coordinator: Python 3.11+, `pyyaml`. Target instances: `awscli`, GROMACS (baked
into the images). On-prem arm: Slurm (`sbatch`, `sacct`).
