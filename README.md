# aws-gromacs

[![lint](https://github.com/scttfrdmn/aws-gromacs/actions/workflows/lint.yml/badge.svg)](https://github.com/scttfrdmn/aws-gromacs/actions/workflows/lint.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![status: pre-run](https://img.shields.io/badge/status-pre--run%20(syntax%20validated)-orange.svg)](#status)
[![code style: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

A harness (`gromacs-bench`) for measuring GROMACS **$/result** and
**time/result** across AWS instance families, local hardware, and on-prem
clusters — provisioned ephemerally through [spore.host](https://spore.host)
(`truffle`, `spawn`, `lagotto`), self-terminating on TTL/idle.

> **This is an argument with measurements attached, not a benchmark suite.**
> Many of its design choices look simplifiable and are not — each protects a
> specific claim. **Read [`THESIS.md`](THESIS.md) first**, then
> [`PLAN.md`](PLAN.md). A locally-sensible "improvement" will quietly destroy
> the argument.

The core claim, compressed: the batch-allocation model silently shaped
molecular-dynamics methodology, and the field now mistakes those constraints for
scientific requirements. `ns/day` is an artifact of how allocations are
requested; the thing anyone actually needs is a **converged observable**. Hold
budget or a deadline fixed, pull the levers the cloud actually offers (width on
demand, heterogeneity, elasticity, generational choice, spot economics), and the
right answers change — sometimes toward cloud, sometimes toward the box under
your desk, sometimes toward an `.mdp` edit.

## Status

Nothing here has been executed against live infrastructure. Every file is
syntax-validated; behavior is not. Two categories of assumption must be
confirmed before a live run:

1. **All `# SPORE:` marked CLI invocations are guessed syntax** (`spore.py`,
   `providers.py`). Confirm against the installed `truffle` / `spawn` /
   `lagotto` before spending money.
2. **`providers._epoch()`** assumes `sacct` emits `%Y-%m-%dT%H:%M:%S` in local
   time. Verify per site, or every on-prem wait number is wrong.

Work is tracked in [issues](https://github.com/scttfrdmn/aws-gromacs/issues) and
on the [project board](https://github.com/users/scttfrdmn/projects/58),
organized by milestone (harness phase) and label (`tier-N`, `capability-gap`,
tool, `load-bearing`/`blocker`/`deferred`).

## Layout

| Path | Role |
|------|------|
| `THESIS.md` | Why the harness is shaped this way — **read first** |
| `PLAN.md` | The spec: cells, protocol, phasing, guardrails |
| `PLAN-cost-per-result.md` | The 20-demo series this harness feeds (D1–D20) |
| `GAPS.md` | Capability gaps (G1–G9), written as issue text |
| `matrix.yaml` | Workloads × instances × configs |
| `run_matrix.py` | Coordinator; writes `results/results.csv` |
| `providers.py` | aws / local / onprem execution + wait measurement |
| `spore.py` | `truffle` / `spawn` wrappers |
| `mdrun_wrapper.sh` | On-target runner (single / MIG / MPS paths, N replicates) |
| `parse_log.py` | ns/day extraction, per-replicate mean + 95% CI |
| `build/` | Five GROMACS images (Intel, AMD ×2, ARM, CUDA) |

## Quick start

```bash
pip install -r requirements.txt

# 1. Inspect the cross product (must show 90 cells) — no spend
DRY_RUN=1 python run_matrix.py --list

# 2. Full dry sweep — no spend
DRY_RUN=1 python run_matrix.py

# 3. ONE real cell end-to-end (small / c8g / cpu-base), then CONFIRM teardown
python run_matrix.py --phase1
```

See [Execution order](#execution-order) before running anything that spends
money. Fill the `CHANGE-ME` placeholders in `matrix.yaml` first.

## Execution order

**Phase 0 — prep** (nothing runs on a cell until this is done):

1. Fill `matrix.yaml`: `s3_bucket`, `region`, the five `images:` URIs, and
   `amortized_hr` for any local arms (write the assumption down).
2. Build and push the five images from `build/`. `mdrun_wrapper.sh` must land at
   `/opt/bench/mdrun_wrapper.sh` inside each.
3. Stage `.tpr` files to `s3://<bucket>/gromacs-bench/tpr/` — two per workload,
   base plus an HMR variant (`<system>-hmr.tpr`, repartitioned masses, 4 fs
   `dt`). **Nothing here generates these** — build them with `grompp`.
4. Raise service quotas for GPU and G/VT spot if needed.

**Phase 1 — validate.** Run `--phase1` (one real cell) and confirm the instance
actually terminated before fanning out.

**Phase 2 — sweep.** `python run_matrix.py` runs the full matrix.

**Phase 3 — findings.** `results/results.csv`, then the decision table in
[`PLAN-cost-per-result.md`](PLAN-cost-per-result.md).

**Phase 6 — generational ladder.** Deferred, not optional. See *Deferred axis*
in [`PLAN.md`](PLAN.md).

## Rules that are load-bearing

- **Run `DRY_RUN=1 python run_matrix.py` after every edit to `run_matrix.py`.**
  Expected: **90 cells / 90 rows**. A past edit orphaned the success path inside
  an `except` block — valid Python, silently unreachable — and only the dry run
  caught it.
- **Every cell runs ≥3 timed replicates and reports a 95% CI.** ns/day is noisy;
  one run is an anecdote. `results.csv` carries `ns_day_ci95` / `ns_day_rel_ci`,
  and when two arms' intervals overlap the ranking between them is not a finding.
  (A CI on the performance proxy — separate from `$/result` convergence, which
  stays downstream.)
- **This harness emits `ns/day` and `$/ns`, never `$/result`.** `$/result` needs
  a convergence criterion declared in advance; a half-defined one here reproduces
  the exact error the thesis is about.
- **`wait_s = acquire_s + provision_s`, symmetric across providers.** The
  scheduling-model comparison lives in that split; do not collapse it. Cloud is
  not exempt from `infeasible:capacity`.
- **Infeasible cells are rows, not gaps.** "Could not run it at all" is a
  finding; the typed reason is the argument.
- **Timing runs are on-demand.** Spot interruption pollutes timing; spot price
  enters only through the `ns_per_dollar_spot` column.
- **Publish the arms that lose.** HMR, a SIMD build flag, or local hardware may
  beat every cloud decision — those rows are what make the rest credible.
- **The framing is "here is the table," never "cloud wins."** See the
  failure-modes list at the end of [`THESIS.md`](THESIS.md).

## Development

Linting is idiomatic and enforced in CI ([lint.yml](.github/workflows/lint.yml)):

| Tool | Scope | Config |
|------|-------|--------|
| [ruff](https://github.com/astral-sh/ruff) | Python lint | `pyproject.toml` |
| [shellcheck](https://www.shellcheck.net/) | `mdrun_wrapper.sh` | — |
| [yamllint](https://yamllint.readthedocs.io/) | YAML | `.yamllint` |
| [markdownlint](https://github.com/DavidAnson/markdownlint) | Markdown | `.markdownlint.yaml` |

Run everything locally via [pre-commit](https://pre-commit.com/):

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

Auto-*formatting* is intentionally **not** enforced: several modules are
hand-aligned (the Student-t table in `parse_log.py`, the aligned dicts in
`run_matrix.py`) for readability. `ruff check` (lint) is the gate; `ruff format`
is opt-in. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Dependencies

Coordinator: Python 3.11+, `pyyaml`. Target instances: `awscli`, GROMACS (baked
into the images). On-prem arm: Slurm (`sbatch`, `sacct`).

## License

[Apache License 2.0](LICENSE) © 2026 Scott Friedman.
