# GROMACS price-performance sweep on AWS via spore.host

> Why this is shaped this way: **`THESIS.md`**. Several choices below look
> simplifiable and are not — the wait/runtime split, symmetric cloud/on-prem
> wait decomposition, infeasible-cells-as-rows, on-demand timing, and the
> refusal to emit `$/result` each protect a specific claim.

**Goal:** For each `workload × instance × config` cell, measure steady-state GROMACS
`ns/day` and derive `ns/day per dollar` (on-demand and spot). Every instance is
launched ephemeral through spore.host and self-terminates on TTL/idle.

## Deliverable
A single `results.csv`. Each `ns_day_total` is the mean of ≥3 timed
replicates and carries its own 95% CI, so every ns/$ number downstream inherits
a defensible error bar:

| workload | atoms | instance | class | config | provider | sims | replicates | ns_day_total | ns_day_ci95 | ns_day_rel_ci | ns_day_per_sim | od_hr | spot_hr | ns_per_$_od | ns_per_$_spot |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

Findings fall out of the table: where GPU beats CPU on ns/$, where c8g (Graviton)
wins the small tier, where g7e whole-card vs 2x/4x MIG lands as atoms climb,
whether AMD c8a beats Intel c8i on ns/$, and whether a build flag (SIMD width) or
an `.mdp` change (HMR) beats every hardware decision in the sweep.

Multi-node is out of scope for now, but when it isn't: `hpc8a` (same Turin
silicon, EFA) is the AMD arm, against `hpc7g` on the Graviton side.

## Workload ladder
Chosen to span the launch/gap-bound → bandwidth-bound regimes so the numbers
discriminate between instances.

| id      | system            | ~atoms  |
|---------|-------------------|---------|
| small   | benchMEM          | 82k     |
| medium  | solvated channel  | ~500k   |
| large   | benchRIB          | 2M      |

Stage the three `.tpr` files to `s3://<bucket>/gromacs-bench/tpr/` once
(Phase 0). Source: Max Planck / UEABS benchmark sets.

## Cells: workload x instance x config
A cell varies three axes, not two. The config axis is the point: there is more
than one way to run GROMACS on the same silicon, and those choices are often
larger than the hardware choice. Configs fold in demos D1 (HMR), D2 (force
placement), D3 (right-size, incl. local hardware) and D13 (MIG carve vs MPS)
from `PLAN-cost-per-result.md`. Campaign-shaped demos (D4-D8, D12, D15) stay
separate and *consume* this table as their placement/pricing input.

### Instances
Defined in `matrix.yaml`. `provider: local` arms run in place at an amortized
$/hr you supply -- write the assumption down.

- CPU Intel — `c8i` (AVX-512)
- CPU AMD — `c8a` (EPYC Turin / Zen 5, 4.5 GHz, 33% more memory bandwidth than
  c7a). Two arms: an AVX-512 build and an `AVX2_256` build on identical silicon,
  because on AMD the SIMD build choice has historically been worth more than the
  instance choice. If that holds, it belongs in the table next to HMR as another
  "the software decision beat the hardware decision" row.
- CPU Graviton — `c8g` (Graviton4, SVE2)
- GPU — `g6` (L4), `g6e` (L40S), `g7e` (RTX PRO 6000 Blackwell, whole card)
- Local — RTX 5090, DGX Spark (amortized $/hr)

### Configs
- `cpu-base` / `gpu-resident` — baselines
- `gpu-cpu-pme` — PME back on the CPU (D2)
- `hmr` / `cpu-hmr` — 4 fs timestep via mass repartitioning (D1); needs a
  separate `<system>-hmr.tpr` built with `grompp`
- `mig2` / `mig4` — 2x `2g.48gb`, 4x `1g.24gb`, one sim per slice (D13)
- `mps4` — 4 sims sharing one card via MPS, for comparison against the carves

`applies_to` gates each config to instance classes or ids; `skip:` prunes
combinations not worth the spend. Guard the cross product -- check
`--list` before running.

## Benchmark protocol (what makes the numbers trustworthy)
- Fixed `-nsteps` with `-resethway` → timers reset past load-balancing warmup;
  report steady-state only. `-noconfout` to skip final-frame write.
- GPU cells: `-nb gpu -pme gpu -bonded gpu -update gpu`.
- CPU cells: fill cores, `-pin on`.
- MIG cells: one `mdrun` per slice pinned via `CUDA_VISIBLE_DEVICES=MIG-<uuid>`,
  run concurrently, sum ns/day.
- **Every cell runs ≥3 timed replicates** (`replicates:` in `matrix.yaml`).
  ns/day is noisy run-to-run — thermal throttling, noisy neighbours, the PME
  auto-tuning draw, DVFS, spot placement — so a single run is not a
  measurement. `parse_log.py` reports the mean and a Student-t **95% CI**;
  `results.csv` carries `ns_day_ci95` and `ns_day_rel_ci`. A cell with a wide
  CI is flagged, not hidden — when the CI on two arms overlaps, the ranking
  between them is *not* a finding, and the table must not pretend it is.
  - This is a CI on the **performance proxy**, not scientific convergence. It
    says "this instance really does this many ns/day," nothing about whether
    the observable converged. `$/result` convergence stays downstream
    (`PLAN-cost-per-result.md`); do not conflate the two error bars.
- **Timing runs are on-demand** (spot interruptions pollute timing). Spot price
  is pulled from truffle and applied only to the ns/$ column.

## Build prep — do this first (the real gotcha)
One image will not cover the matrix. Build five (`build/`):
- `Dockerfile.x86`      → Intel, `GMX_SIMD=AVX_512`
- `Dockerfile.amd`      → Zen 5, `GMX_SIMD=AVX_512`, `-march=znver5`
- `Dockerfile.amd-avx2` → Zen 5, `GMX_SIMD=AVX2_256` (comparison arm)
- `Dockerfile.arm`  → `GMX_SIMD=ARM_SVE`, `-mcpu=neoverse-v2`
- `Dockerfile.cuda` → `GMX_GPU=CUDA` (used for g6/g6e/g7e, whole-card and MIG)

Push to a registry the spawned instances can pull. Set image URIs in `matrix.yaml`.

## Execution flow (per cell, driven by `run_matrix.py`)
1. `truffle` — resolve type, pull on-demand + spot price, confirm quota.
2. `spawn` — launch on-demand with TTL (2h) + idle timeout, arch-correct image.
3. Stage `.tpr` from S3, run `mdrun_wrapper.sh` (≥3 timed replicates), capture
   the `md_rep*.log` files.
4. `parse_log.py` — per replicate, sum ns/day across MIG slices; across
   replicates, report mean + 95% CI; compute ns/$ from the mean.
5. Append row (incl. `replicates`, `ns_day_ci95`); instance auto-terminates.

Claude Code is the coordinator. It can call the truffle/spawn CLIs directly (as
wrapped in `spore.py`) or drive them via the spore.host MCP server — either way
the loop, parsing, and CSV assembly are the same.

## Deferred axis: generational depth (do this, but not first)

**Status: deferred. Not Phase 0-3. Do it before the work is called done.**

`$/hr` is a menu proxy exactly as `ns/day` is a performance proxy -- it is what
you can see before you run, so it is what people optimize. The generational
ladder is the cleanest demonstration that the two rankings differ: something
cheaper on the menu is not cheaper on the result.

### Why the curve shifts (three different directions, keep them separate)

1. **Old gen loses on $/result.** Per-core throughput and memory bandwidth gains
   outrun the price delta. Less per hour, more per nanosecond.
2. **Old gen wins.** A small system that cannot fill new silicon collects no
   generational benefit, so it collects the discount for free. This is the same
   argument as the utilization one, one level out.
3. **Old gen wins for a non-performance reason.** Demand has moved on, so spot
   pools are deeper and interruption hazard lower. On ensemble work that can
   exceed the speed difference, and it appears on no spec sheet.

### Why it belongs in the feasibility column too
Generational depth is itself a cloud-only capability. On-prem you have the
generation you bought. That is an `infeasible:hardware` row -- the site cannot
run the comparison at all, which demonstrates more than winning it would.

### Ladder (instances only; images are already built)
Cheap to add: generations are additional instance rows sharing an existing
image. No harness changes expected.

- Intel: `c5` / `c6i` / `c7i` / `c8i`
- AMD: `c6a` / `c7a` / `c8a`
- Graviton: `c6g` / `c7g` / `c8g`
- GPU: `g4dn` / `g5` / `g6` / `g6e` / `g7e`

Prune aggressively -- the full ladder x workload x config is not worth the
spend. Two or three rungs per family at two workload sizes is enough to show a
crossing.

### The actual deliverable
Plots, not rows. Per workload size, one line per family:
`$/hr` ranking vs `$/result` ranking, and where they cross as atom count climbs.
The crossing point is the finding. Also plot spot depth / interruption hazard
per generation, since reason (3) is invisible otherwise.

### Sequencing
Slot as **Phase 6**, after the demo series. It depends on nothing, which is
exactly why it is easy to defer -- and easy to forget. It is listed here so it
is not.

## Phasing + guardrails
- **Phase 0** — build 5 images (incl. HMR `.tpr` variants), stage workloads, run
  truffle price/quota checks.
- **Phase 1** — one cell end-to-end (`small` on `c8g`) to validate harness *and*
  teardown before fanning out.
- **Phase 2** — full sweep.
- **Phase 3** — assemble `results.csv`, derive findings.
- **Phase 6** — generational ladder (see *Deferred axis* above). Depends on
  nothing, which is why it is easy to defer and easy to forget. Do not skip it.
- Every instance carries TTL + idle timeout; cap concurrent instances; verify
  termination after each phase. Start with `--dry-run`.

## Fill-in checklist before running
- [ ] `S3_BUCKET`, registry URIs in `matrix.yaml`
- [ ] Confirm actual truffle/spawn flag syntax in `spore.py` (placeholders marked `# SPORE:`)
- [ ] `.tpr` files staged to S3
- [ ] Three images built + pushed
- [ ] Service quotas raised for GPU + G/VT spot if needed
- [ ] `replicates` set (≥3) — budget for it; a cell now costs N timed runs, and
      the TTL must cover N × single-run time. A wide CI on Phase 1 means raise N
      or investigate the noise before trusting the sweep.
