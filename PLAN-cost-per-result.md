# $/result: a demonstration series

**Thesis: see `THESIS.md`.** It stays unstated in the published artifact — the
table argues, not the prose. Compressed: ns/day is an artifact of the batch
allocation model, and the thing anyone actually needs is a converged observable.
Optimize for that and the right answers change — sometimes toward cloud,
sometimes toward the box under your desk, sometimes toward an `.mdp` edit.

**Method:** never argue. Run matched-budget head-to-heads, publish the table,
let the reader draw the conclusion. Output is a lookup: *if you want X, do Y.*

---

## Metric definitions (fix these before any run)

- **Result** = an observable meeting a convergence criterion declared *in
  advance*. Never "a trajectory." Candidates:
  - ΔG binding, converged to ±0.5 kcal/mol (bootstrap CI over independent ensembles)
  - MSM implied timescales stable within X% across lag times
  - Per-residue RMSF converged to a fixed tolerance
  - Pose retention fraction, CI half-width below threshold
- **$/result** — total incurred cost (compute + storage + egress + wasted/
  interrupted work), not list price of the winning run.
- **time/result** — wall-clock from submit to converged answer, *including*
  queue wait for the on-prem arms. This is the less-charged framing and often
  the more damning one.
- **Pre-registration.** Write the convergence criterion and the budget into the
  run config before executing. Otherwise every comparison is cherry-picked and
  the whole exercise is worthless.
- **Error bars or it didn't happen.** Repeat each arm ≥3× with different seeds;
  report spread. A single-shot demo is a marketing asset, not evidence.

---

## Demo series

Each demo is: fixed budget (dollars or wall-clock), two or more arms, one
declared observable. Report $/result and time/result. Nothing else.

### Tier 1 — free money, no cloud argument required
Run these first. They cost nothing to demonstrate and they buy credibility,
because they show you're not selling cloud.

**D1. Hydrogen mass repartitioning.**
4–5 fs timestep vs 2 fs, same observable. Near-2× for an `.mdp` change and a
`grompp` flag. If this beats every hardware decision in the series — and it may —
say so loudly and first.
*If you want more sampling: fix your timestep before you buy anything.*

**D2. Force-placement tuning.**
`-nb/-pme/-bonded/-update` combinations, plus GPU-resident mode, on one card.
Same hardware, same system, measured spread.
*If you want more ns/day: tune placement before changing instances.*

**D3. Right-size the card.**
Identical system on g7e / g6e / g6, and on local hardware (RTX 5090, DGX Spark).
Report $/result and results/day. Expect the small card to win $/result outright
for systems that don't fill the big one — and the local box to win both for
anything small enough to fit.
*If you want cheap results on a small system: run it locally, or on the smallest
card that saturates.*

### Tier 2 — the structural argument
These are the demos that can't be run under a fixed allocation at all.

**D4. Depth vs width.** *The centerpiece.*
Matched budget. Arm A: one long trajectory. Arm B: N short trajectories,
independently seeded, placed across spot capacity. Which converges the declared
observable first? For most observables B wins, and B is unrunnable on a fixed
allocation without reserving peak width for the full duration.
*If you want a converged observable: buy width, not depth.*

**D5. Adaptive vs uniform placement.**
Same budget, same width. Arm A: uniform seeding. Arm B: run a batch, fit an MSM,
place the next batch in under-sampled states. Concurrency varies as a function of
intermediate results — a control loop, not a job.
*If you want conformational coverage: close the loop.*

**D6. Variance-allocated FEP.**
Uniform compute per λ window vs allocation proportional to per-window variance.
Same total budget, tighter final CI.
*If you want a binding free energy: stop giving every window the same time.*

**D7. Sequential stopping.**
Kill ensemble members once the aggregate observable meets criterion, vs running
all members to a fixed length. Measures how much of a conventional ensemble is
pure waste.
*If you want to stop paying: instrument convergence and terminate early.*

**D8. Multi-fidelity triage.**
Coarse/short screening pass over many candidates, expensive runs only on
survivors. Report $/correct-hit against the all-expensive baseline.
*If you want to rank 500 ligands: don't run 500 expensive simulations.*

### Tier 3 — operational levers
Less headline, real money, and they're what makes Tier 2 executable.

**D9. Shard length vs spot economics.**
Sweep work-unit length (10 min → 8 h) against measured per-pool interruption
hazard. Plot $/result. Locate the point where checkpoint machinery stops earning
its keep and re-running the shard is simply cheaper. Compare Daly's optimal
interval against the measured optimum.
*If you want spot to be boring: make the shard short enough that interruption is
a rounding error.*

**D10. Worker-pull vs instance-per-shard.**
Long-lived workers draining a shard queue vs a fresh instance per shard.
Isolates boot + image pull + PME retune as a tax; shows amortization.
*If your shards are short: amortize the boot.*

**D11. Heterogeneous placement.**
Single-family spot vs simultaneous bidding across g6/g6e/g7e/c8g and multiple
AZs/regions, scored by the ns/$ table from the benchmark matrix. Members finish
at different rates; only the aggregate matters.
*If you want capacity and price: stop insisting every member match.*

**D12. Straggler hedging.**
Time-to-last-result with and without speculative duplication of long-running
shards. Attacks ensemble tail latency directly.
*If you want predictable wall-clock: duplicate the stragglers, eat the waste.*

**D13. Asymmetric MIG co-tenancy.**
Uniform 4× `1g.24gb` vs mixed carve (one `2g.48gb` + two `1g.24gb`) with
deliberately mismatched systems — one bandwidth-hungry, two launch-bound.
Tests whether their idle gaps and bandwidth demand anti-correlate. Also compare
MPS at equivalent packing.
*Open question, not a claim. The benchmark matrix settles it.*

**D14. Split the pipeline.**
`grompp`/`trjconv`/analysis on c8g spot, only the `.tpr` shipped to the GPU, only
frames shipped back — vs one instance doing everything.
*If you want to stop wasting the expensive part of the bill: separate resource
classes.*

**D15. Shared equilibration prefix.**
One equilibration, N production branches vs N independent full pipelines.
Measures redundant work in a conventional ensemble setup.

**D16. Frame policy.**
Compute observables in-flight and write scalars, keeping sparse frames as
re-analysis insurance — vs writing everything. Include storage and egress in
$/result. Caveat honestly: does not apply to exploratory work where you don't yet
know what you'll measure.
*If storage is your line item: stop writing frames you'll never read.*

**D17. Queue latency.**
Time-to-result including realistic on-prem queue wait, against cloud
time-to-result. The honest version measures the human loop: submit → wait →
look → think → resubmit. Cheap iteration changes which questions get asked.
*If you want iteration speed: the bottleneck was never FLOPs.*

### Tier 4 — where this loses
Publish these in the same table, not as a footnote. Leading with the failures is
what makes the rest credible.

**D18. The HPC-shaped problem.**
One tightly-coupled multi-million-atom system needing many nodes of EFA. Expect
the on-prem cluster (or a tightly-coupled cloud cluster) to win. Show it.

**D19. Sustained utilization.**
A stable 24/7 workload amortized over three years on-prem vs cloud list. Expect
on-prem to win. Show it.

**D20. Data gravity.**
A corpus already resident in a machine room; include egress and transfer time.
Expect cloud to lose. Show it.

---

## Deliverable

**The artifact is a decision table, not an essay.** Rows are goals, columns are
the prescription and the measured evidence:

| If you want… | Do this | $/result | time/result | Demo |
|---|---|---|---|---|
| A converged observable on a budget | width, not depth; spot ensemble | | | D4 |
| Conformational coverage | adaptive placement loop | | | D5 |
| A binding ΔG | variance-allocated λ windows | | | D6 |
| To rank many candidates | multi-fidelity triage | | | D8 |
| More sampling, no new hardware | HMR + placement tuning | | | D1, D2 |
| Cheap results, small system | run it locally | | | D3 |
| Predictable ensemble wall-clock | hedge stragglers | | | D12 |
| Reproducible per-run timing | MIG slices | | | D13 |
| One huge tightly-coupled run | a real cluster with EFA | | | D18 |
| 24/7 steady-state throughput | buy the machine | | | D19 |

Supporting material: the benchmark matrix (`results.csv`) as the ns/$ spine, and
the harness itself so every row is reproducible.

## Sequencing

1. **Phase 0** — finish the instance benchmark matrix. It's the denominator for
   everything here.
2. **Phase 1** — Tier 1 (D1–D3). Cheap, fast, and they establish that you'll
   report against your own interest.
3. **Phase 2** — D4 alone. It's the centerpiece; if it doesn't hold up, the
   thesis needs rework before anything else is built.
4. **Phase 3** — Tier 3 operational levers, since they're what makes Tier 2 runnable.
5. **Phase 4** — D5–D8 (the closed-loop demos), which need the shard/queue
   infrastructure from Phase 3.
6. **Phase 5** — Tier 4 failures, then assemble the table.

## Guardrails

- Pre-register criterion + budget per arm. No post-hoc threshold selection.
- Same GROMACS version, same force field, same system across arms of a demo.
- Report incurred cost including waste, not the cost of the successful path.
- Publish the arms that lose.
- Where a result depends on a regime (system size, observable, ensemble shape),
  say so — a prescription without its boundary is just marketing.
