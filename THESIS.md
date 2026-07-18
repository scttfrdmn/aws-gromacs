# THESIS — read this before changing anything

`PLAN.md` says what to build. This says **why it is shaped this way**, because
most of the harness's odd choices are load-bearing for an argument, and a
locally-sensible "improvement" will quietly destroy it.

---

## The claim

**The batch-allocation model silently shaped molecular dynamics methodology, and
the field now mistakes those constraints for scientific requirements.**

On a shared cluster you request N nodes for T hours, and you must request them
*before you know anything*. That single fact forces every algorithm into a
static rectangle: fixed width, fixed duration, committed in advance. Methods
that fit the rectangle flourished. Methods that did not were never developed,
never taught, and eventually became unthinkable rather than merely impractical.

The clearest casualty is the figure of merit. The field optimizes **ns/day** —
one long trajectory, strong-scaled until the interconnect gives out. But nobody's
paper needs a fast trajectory. It needs a **converged observable**. Those two
things come apart badly, and almost every downstream pathology follows from
optimizing the proxy instead of the goal:

- Buying a flagship GPU for a system too small to fill it, because "newer is
  faster" is how you optimize ns/day.
- Running one long trajectory when five short ones would converge the answer
  sooner, because width was never purchasable.
- Reporting simulation length rather than statistical convergence.
- Treating 15% GPU utilization as normal, because the alternative — right-sizing
  down, or packing an ensemble onto one card — is not expressible in a batch
  request.

The point is *not* that researchers are careless. The constraint is invisible
precisely because it is universal. When every machine you have ever used works
this way, its limits read as the nature of the problem rather than the shape of
the procurement.

---

## What this harness is actually for

Not "cloud is cheaper." Per core-hour it usually is not, everyone in research
computing knows it, and leading there loses the room immediately.

The harness exists to **replace an argument with a table**. Two denominators,
both chosen deliberately:

- **`$/result`** — dollars per converged observable, not per nanosecond.
- **`time/result`** — often the better one, because most on-prem sites cannot
  produce a defensible `$/hr` and do not want that fight. Time they can measure,
  and it forces queue wait into the denominator, which is where the real
  difference lives.

And one axis that is not a number at all: **feasibility**. Some comparisons
cannot be run under a fixed allocation *at all*. That is a stronger result than
winning them.

### The question is "how much can I get done," held fixed

`$/result` is one direction of a single question. Turn it around and it is the
one a researcher actually asks: **for a fixed budget — dollars, or a wall-clock
deadline — how much science can I get done?** Fix the denominator and the whole
comparison becomes concrete. Every demo in `PLAN-cost-per-result.md` is a
matched-budget head-to-head for exactly this reason.

The point this harness is built to demonstrate is what the cloud can do **when
you stop reproducing the batch allocation and think fresh** — because the cloud
is not one lever, it is many, and the batch model let you pull almost none of
them:

- **Width on demand** — buy 500 concurrent instances for an hour, not 4 nodes
  for a week. Depth-vs-width (D4) is the whole game.
- **Heterogeneity** — bid across g6/g6e/g7e/c8g and many AZs at once (D11);
  right-size the card to the system instead of buying the flagship (D3).
- **Elasticity** — vary concurrency *as a function of intermediate results*: a
  control loop, not a job (adaptive sampling D5, sequential stopping D7).
- **Generational choice** — pick the generation whose `$/result` wins, not the
  one that was purchased three years ago (Phase 6).
- **Spot economics** — shard length tuned against measured interruption hazard
  (D9), stragglers hedged (D12).

Held against a fixed budget or deadline, these levers change *how much gets
done*, not just the per-hour price. That is the finding. "Cloud is cheaper per
core-hour" is not — and usually isn't true — so we never lead with it.

---

## Why specific design choices exist

Each of these looks like it could be simplified. None of them can.

### Wait and runtime never share a column
Runtime is a hardware claim. Wait is a **scheduling-model claim**, and the
scheduling model is the thesis. Collapsing them into one number destroys the
only measurement that distinguishes an allocation model from a machine.

### Cloud gets the same wait decomposition as on-prem
Cloud has a queue too — contended capacity, exhausted spot pools, quota
ceilings. A request that sits unfulfilled is a queue by any honest definition.
`acquire_s` / `provision_s` are measured symmetrically on **both** sides, and
cloud is not exempt from `infeasible:capacity`. If the comparison is rigged in
cloud's favor, nobody in research computing will believe any of it, and they
will be right not to.

### Infeasible cells are rows, not gaps
`outcome: infeasible:<class>` with a typed reason. The classes argue different
things and are not interchangeable:

| class | argues |
|---|---|
| `capacity` | procurement |
| `hardware` | procurement |
| `latency` | scheduling model |
| `fit` | sizing |
| **`elasticity`** | **methodology — this is the thesis** |

`elasticity` means *width cannot vary mid-campaign*. Adaptive sampling (D5) is
not slow under a fixed allocation; it is **unexpressible**. An empty cell with a
typed reason argues better than any number you could place beside it, and it
cannot be rebutted with a better procurement deal.

### On-prem is a provider, not a rival
`provider: onprem` submits the same wrapper through the site scheduler. A site
**adds its own arm** rather than disputing yours. This is much harder to dismiss
than any comparison you run on their behalf.

### Queue wait is reported as best / p50 / p90
Give away the favorable framing up front — for every arm, not just the cloud
one. The first person who says "you used worst-case queue times" wins the
exchange, so pre-empt it. `time_to_result_best_s` uses the most generous wait
available.

### Timing runs are on-demand
Spot interruption pollutes timing. Spot price enters only through the
`ns_per_dollar_spot` column. Switching timing runs to spot to save money looks
thrifty and silently corrupts the measurement.

### The harness emits ns/day, and must not emit $/result
`ns/day` is not a result. `$/result` requires a convergence criterion per
observable, declared in advance. Emitting a half-defined `$/result` from this
harness would reproduce the exact error the thesis is about: optimizing a proxy
because it is the number that is easy to get.

### The losing arms are the point
Hydrogen mass repartitioning (D1) may be the single largest win in the entire
series, and it is an `.mdp` change with nothing to do with cloud. A SIMD build
flag on AMD may beat every instance choice. Local hardware may beat cloud
outright on small systems. **Publish these at the same volume as the wins.**
They are what make the rest credible — and they are also true, which matters
more.

---

## The terminal framing

One level above cloud-vs-cluster, which is the argument worth having:

> **Stop optimizing proxies. Measure results.**

Cloud wins many of those comparisons because the proxy everyone optimizes was
shaped by an allocation model. But not all of them — and the ones it loses are
precisely what make anyone believe the ones it wins.

The rhetorical move that lands is not an essay. It is two runs at the same cost:
one 10 µs trajectory, versus 500 × 100 ns adaptively placed. Ask which produced
more science. For most observables the second wins outright, and it is
**unrunnable** under a fixed allocation.

---

## Where this loses — publish these in the same table

Not footnotes. Leading with the failures is what earns the rest a hearing.

- **Tightly-coupled multi-node.** One huge system needing many nodes of
  low-latency interconnect is an HPC-shaped problem. The cluster wins. (D18)
- **Sustained 24/7 utilization.** Stable workloads amortized over three years are
  cheaper on-prem. Full stop. (D19)
- **Data gravity.** If the corpus already lives in a machine room, egress and
  transfer time can dominate everything else. (D20)

---

## Failure modes for whoever executes this

Each of these is a locally-reasonable decision that destroys the argument:

1. **Reporting ns/day as the headline.** It is an intermediate quantity. The
   headline is `$/result` and `time/result`.
2. **Dropping infeasible rows because they look like missing data.** They are
   the strongest cells in the table.
3. **Merging `acquire_s` and `provision_s` for tidiness.** Deletes the
   scheduling-model measurement.
4. **Running timing on spot to cut cost.** Corrupts the timing.
5. **Quietly omitting arms that lose.** Destroys credibility, which is the only
   asset this work has.
6. **Framing results as "cloud wins."** The framing is "here is the table."
   Anything stronger invites — and deserves — dismissal.
7. **Skipping pre-registration of convergence criteria.** Without a threshold
   declared in advance, every comparison is cherry-picked and the whole exercise
   is worthless.
8. **Treating the harness as a benchmark suite.** It is an argument with
   measurements attached. Optimizing it for coverage or throughput at the cost
   of the distinctions above is a net loss.
9. **Reporting single-shot ns/day, or ranking two arms whose CIs overlap.**
   ns/day is noisy; one run is an anecdote. Every cell is ≥3 timed replicates
   with a 95% CI (`ns_day_ci95`), and when two arms' intervals overlap the
   ordering between them is *not* a finding — say so rather than ranking noise.
   This is a CI on the performance proxy; it is **not** a substitute for
   pre-registered `$/result` convergence, which is a separate error bar
   downstream. Do not let one stand in for the other.
