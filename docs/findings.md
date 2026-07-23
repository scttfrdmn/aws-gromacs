# Findings

Measured results as they land. Each is a real number from the harness (≥3 timed
replicates, 95% CI), not an assertion. Raw data: `results/` + S3
`gromacs-bench/results/`. This file is the running record behind the eventual
decision table (`PLAN-cost-per-result.md`).

Region: us-east-1. On-demand pricing (timing runs are on-demand; spot enters
only via the ns/$ spot column). GROMACS 2025.2.

---

## F4 — The GPU-vs-CPU verdict FLIPS with system size; the crossover is real but narrow once the CPU is tuned (D3, regime boundary)

**Systems:** benchMEM (82k, from F1) and the new `medium` system — a KcsA K⁺-channel
tetramer in a POPC bilayer, 351k atoms, built in CHARMM-GUI (CHARMM36m). This is the
regime boundary F1 explicitly flagged as "still to be measured."

**Best CPU vs best available GPU, ns/day (n=3, 95% CI):**

| system (atoms) | best CPU (build) | GPU (g6e L40S, gpu-prod) | winner |
|----------------|------------------|--------------------------|--------|
| benchMEM (82k) | c8a Zen5 **270.6 ± 2.9** (F1) | L4 **182.3 ± 1.5** (F1) | **CPU, 1.48×** |
| medium (351k)  | c8a Zen5 tuned **41.28 ± 0.27** | L40S **49.51 ± 0.18** | **GPU, 1.20×** |

**Three measured findings:**

1. **The verdict flips with system size — exactly as F1's utilization argument
   predicted.** On the 82k system the CPU wins (1.48×): too small to feed the card
   (F1 measured 19% SM-active on the L4). On the 351k system the GPU wins (1.20×):
   now large enough to saturate it. Same workload class (membrane protein), opposite
   answer — the discriminator is system size, not "GPU = fast." CIs disjoint in both
   regimes, so both directions are real findings.

2. **The GPU's win is NARROW (1.20×) once the CPU gets its best build — and that
   changes the decision.** Against the CPU *floor* build (c8a AVX2, 33.79) the GPU
   looks 1.47× faster; against the *tuned* CPU (c8a native AVX-512, 41.28 — the +23%
   from F3) it is only 1.20×. At 1.20×, the GPU's premium and its **availability**
   can tip the answer back to the CPU: in this run the L40S (g6e) was the *only* GPU
   that launched — **g6 and g7e both returned `infeasible:capacity`** after retries.
   `time/result = wait + runtime`: a 20% faster runtime you cannot get capacity for
   loses to a CPU you can launch now. Reporting the GPU-vs-*floor* ratio would have
   overstated the GPU by ~25% — the honest comparison is against the tuned CPU.

3. **Moving PME off the GPU throws away the entire GPU advantage.** `gpu-cpu-pme`
   (PME on CPU) measured 14.36 ± 0.20 ns/day on the L40S — *slower than every modern
   CPU rung* (c8a 33.8, c8i 26.3, m9g 26.6) and 3.4× slower than `gpu-prod` on the
   same card. On this system the PME offload *is* the GPU's value; a config that
   keeps PME on the CPU should just use a CPU.

**Conclusion.** "Get on the GPU" is wrong for small systems (F1) and right for large
ones, with a measurable crossover between 82k and 351k atoms — but near the crossover
the margin is small enough that CPU build quality, GPU price, and above all GPU
*availability* decide the real time-to-result. The size of your system, the build you
give the CPU, and whether the card even exists in the region are all part of the same
decision. (D3)

*If your system saturates the card, the GPU wins — but only ~1.2× on a mid-size
membrane, so a tuned CPU you can actually launch may still be the better time/result.*

**Data quality:** all cells 3-replicate mean±CI, CIs disjoint for every ranked pair.
CPU medium is the floor build except the explicit `c8a-native` tuned cell (41.28);
GPU is g6e (L40S) — g6/g7e recorded `infeasible:capacity`. benchMEM row is from F1.
The `medium` tpr (`channel-medium.tpr`, 351k atoms, CHARMM36m) is the CHARMM-GUI build
described in docs/divergence.md.

---

## F3 — "Compile for the newest ISA" is wrong on 2 of 3 CPU vendors; the effect grows with generation, opposite signs (Phase 6)

**System:** benchRIB (2M atoms). Three generational ladders — Intel c5→c8i, AMD
c6a→c8a, Graviton c6g→m9g — each run as a **two-build matrix**: a *floor* build
(one portable binary per family: x86 AVX2_256, Graviton ARM_NEON) that isolates
the hardware generation, and a *tuned* build (`-march=native`/`-mcpu=native`
compiled **on** the target chip, so GROMACS auto-selects the best SIMD kernels)
that isolates what compiling-for-the-chip buys. The gap between them is the
finding. All cells are 3-replicate mean ± 95% CI; SIMD level confirmed from each
mdrun log. STREAM = measured triad bandwidth (`preprocess/sysinfo.sh`).

**Floor → tuned, ns/day (n=3, 95% CI):**

| vendor | chip (µarch) | STREAM | floor (AVX2/NEON) | tuned (native) | tuned SIMD | Δ tuned vs floor |
|--------|--------------|--------|-------------------|----------------|-----------|------------------|
| Intel | c5 Cascade | 161 | 5.27 ± 0.52 | 6.36 ± 0.20 | AVX-512 | **+21%** |
| Intel | c6i Ice | 272 | 8.46 ± 0.04 | 9.56 ± 0.02 | AVX-512 | **+13%** |
| Intel | c7i Sapphire | 186 | 7.64 ± 0.001 | 8.81 ± 0.04 | AVX-512 | **+15%** |
| Intel | c8i Granite | 355 | 10.59 ± 0.005 | 12.10 ± 0.10 | AVX-512 | **+14%** |
| AMD | c6a Zen3 | 101 | 6.63 ± 0.01 | *— none —* | (no AVX-512) | **N/A** |
| AMD | c7a Zen4 | 298 | 10.93 ± 0.10 | 11.59 ± 0.04 | AVX-512 | **+6%** |
| AMD | c8a Zen5 | 378 | 16.39 ± 0.02 | 20.15 ± 0.07 | AVX-512 | **+23%** |
| Graviton | c6g G2 | 150 | 3.205 ± 0.003 | *— none —* | (no SVE) | **N/A** |
| Graviton | c7g G3 | 247 | 4.875 ± 0.001 | 4.844 ± 0.002 | SVE | **−0.6%** |
| Graviton | c8g G4 | 381 | 8.10 ± 0.01 | 7.27 ± 0.00 | SVE2 | **−10%** |
| Graviton | m9g G5 | 306 | 10.64 ± 0.01 | 8.97 ± 0.01 | SVE | **−16%** |

**Four measured findings:**

1. **On Graviton, the "better" ISA (SVE) is a REGRESSION for GROMACS, and it gets
   worse on newer silicon.** NEON beats SVE on every Graviton that has SVE:
   G3 −0.6% (essentially tied), G4 −10%, G5 −16% — three chips, all CIs disjoint,
   monotonically worsening. Root cause, confirmed from the mdrun log: the native
   build selects `ARM_SVE` with `-msve-vector-bits=128`. Graviton implements SVE
   at **128-bit vector width — the same width as NEON** — so there is no width
   advantage, and GROMACS's SVE kernel path is less mature than its
   heavily-hand-tuned NEON path. The claim is not "SVE is bad"; it is "SVE at the
   width Graviton ships does not beat NEON for MD, and choosing it costs you more
   on each newer generation."

2. **On x86, AVX-512 HELPS — uniformly on Intel, and increasingly on AMD.** Intel:
   every rung gains (+21/+13/+15/+14% for c5/c6i/c7i/c8i). AMD: the gain *grows*
   with generation — Zen4 +6%, Zen5 +23% (Zen5's full-width AVX-512 datapath vs
   Zen4's double-pumped one). AMD is the mirror image of Graviton: same
   "effect widens with generation," opposite sign.

3. **The AVX-512-downclock hypothesis is REFUTED for this workload.** The textbook
   expectation — enabling AVX-512 on c5 (Cascade Lake) triggers enough frequency
   throttling that the AVX2 floor should win — did not happen: c5 gains +21% from
   AVX-512. On a bandwidth-bound 2M-atom MD run, the wider vectors more than offset
   the modest all-core clock drop. The downclock penalty is real for compute-bound
   AVX-heavy code; MD on CPU is memory-bound, so it does not dominate. (This is why
   the arm was run rather than asserted — the prediction was wrong.)

4. **The oldest chips cannot be tuned up at all.** Zen3 (c6a) has no AVX-512 and
   Graviton2 (c6g) has no SVE, so their portable floor build *is* their maximum —
   there is no tuned arm to run. You cannot compile your way out of old silicon;
   the only lever left is replacing the hardware. (Graviton2 is also near
   operationally unviable for large MD: 3.20 ns/day / 7.5 h per ns, so slow that a
   3-replicate run overran the default idle/TTL and had to be re-run at a 10 h TTL
   — the same "old gen is slow enough to matter operationally" seen for c5 in F2.)

**Conclusion.** The universal on-prem instinct — *compile `-march=native`, use the
newest instruction set the chip advertises* — is **correct on Intel, partly correct
on AMD (small on Zen4, large on Zen5), and actively wrong on Graviton** (SVE loses
to NEON, more so each generation). "Max tuning" is neither free nor monotonic nor
portable: the right build is per-chip, and on two of three vendors here the naive
choice leaves performance on the table or takes it away. In the cloud you pick the
binary as well as the box — and the floor build, not the tuned one, is the right
default on Graviton. All comparisons respect the CI rule; the only overlapping-CI
pair (c7g SVE vs NEON) is reported as a tie, not an inversion.

*If you want CPU throughput on Graviton: build NEON, not SVE. On x86: AVX-512 is
worth it, most on the newest AMD. On any vendor's oldest rung: the build can't save
you.* (Phase 6)

**Data quality:** every Δ above is between two 3-replicate mean±CI cells with
non-overlapping CIs except c7g (−0.6%, a statistical tie reported as such). c5 and
c8i tuned cells carry wider CIs (~3–4%, oldest and re-launched respectively) but
their +21%/+14% gaps dwarf the CI. c6g (Graviton2) floor CI is pending a 10 h
re-run (2 replicates agreed exactly at 3.204 before the first run was reaped).
Floor builds: x86 = `-march=znver3`/generic AVX2_256 (znver3 chosen after
`-march=znver4` was found to emit AVX-512 in compiler codegen and SIGILL on Zen3);
Graviton = `ARM_NEON_ASIMD`/`-mcpu=generic`. Both are one binary across their whole
ladder, so the floor ladders also re-confirm F2's bandwidth story
(AMD 6.63<10.93<16.39, Intel 5.27<8.46<10.59, tracking STREAM). The Graviton floor
ladder rises with generation (G2 3.21 < G3 4.88 < G4 8.10 < G5 10.64) but is NOT
strictly bandwidth-ordered at the top: m9g/G5 (306 GB/s) beats c8g/G4 (381 GB/s),
so on Graviton, core/IPC generation outweighs STREAM at the high end — the reverse
of the AMD/Intel ladders where bandwidth dominates. c6g/G2 took 5.5 h wall-clock
for its 3 replicates (3.205 ± 0.003), reinforcing finding 4.

---

## F2 — Neither generation nor $/hr predicts CPU throughput; memory bandwidth does (Phase 6)

**Systems:** benchMEM (82k). Intel generational ladder c5→c8i, all `.24xlarge`
(96 vCPU). ns/day is rep0 pending 3-replicate CIs; the pattern is far larger than
run-to-run noise. Bandwidth = measured STREAM triad (`preprocess/sysinfo.sh`).

ns/day is 3-replicate mean ± 95% CI where shown (@96c and @48c); STREAM = measured
triad bandwidth (`preprocess/sysinfo.sh`).

| gen | µarch (year) | $/hr OD | STREAM GB/s | NUMA | ns/day @96c |
|-----|--------------|---------|-------------|------|-------------|
| c5  | Cascade Lake (2019) | 4.08 | 161 | 2 | (high-core pending) |
| c6i | Ice Lake (2021)     | 4.08 | 272 | 2 | 143.7 ± 6.7 |
| c7i | Sapphire Rapids (2023) | 4.28 | 186 | **1** | 137.8 ± 4.1 |
| c8i | Granite Rapids (2025) | 4.50 | 355 | 2 | 175.8 ± 3.6 |

**Three measured findings:**

1. **Throughput tracks memory bandwidth, not generation or price.** ns/day rises
   with STREAM GB/s (161→272→355 → 80→141→174), not with generation number or
   $/hr (which rise only ~10% c5→c8i). MD on CPU is bandwidth-bound; the menu
   number ($/hr) and the spec-sheet generation both fail to predict $/result.

2. **The clean per-core staircase COLLAPSES at full width — bandwidth explains it.**
   At 48 cores (all fed) the order is monotonic and CI-clean: c6i 108.5±3.0 <
   c7i 123.3±0.9 < c8i 161.4±3.7 — true per-core generational IPC, c7i clearly
   ahead of c6i. At 96 cores that lead **evaporates**: c6i 143.7±6.7 vs c7i
   137.8±4.1 — the CIs **overlap**, so c7i and c6i are statistically tied at full
   width (NOT a confirmed inversion — rep0 alone suggested c7i<c6i, but with 3
   replicates it is within noise; per the CI rule that ordering is not a finding).
   The real, defensible result: **c7i leads c6i by ~14% at 48 cores but loses
   that lead entirely by 96**, because AWS provisions c7i.24xlarge as a **single
   NUMA node** with the lowest bandwidth of the modern three (186 GB/s, barely
   above 2019's c5). Its cores starve for memory before c6i's (272 GB/s) do. The
   full-width number hides the per-core truth; the curve reveals it.

   **On the LARGE system (benchRIB, 2M atoms, 96 cores) the bandwidth ordering
   is unambiguous** — full generational ladder, CIs non-overlapping:

   | gen | STREAM GB/s | ns/day (n=3) |
   |-----|-------------|--------------|
   | c5  | 161 | 5.96 ± 0.69 (wide, 12%) |
   | c7i | 186 | 8.91 ± 0.02 |
   | c6i | 272 | 9.62 ± 0.01 |
   | c8i | 355 | 12.04 ± 0.10 |

   ns/day follows STREAM bandwidth **exactly** (161<186<272<355 →
   5.96<8.91<9.62<12.04), NOT generation number: the 2023 c7i is genuinely,
   measurably slower than the 2021 c6i on a memory-bound 2M-atom system, because
   AWS gives c7i.24xlarge a single NUMA node / less bandwidth. c8i is 2.0× c5.
   Where the small system left c6i/c7i within noise, the large system — which
   truly saturates memory — separates every rung cleanly. (c5's CI is wide, 12%,
   and the harness auto-flagged it `!wide-CI`: the 2019 chip's benchRIB timing is
   noisier than the modern parts' ±0.01–0.10. It also took **3.1 h** wall-clock
   vs minutes for c8i — old gen can be near operationally unviable for large
   work, not merely slower-per-dollar. An earlier c5-large run hit a 120 min TTL
   before finishing 3 replicates; re-run with a 6 h TTL for this number.)

3. **Every generation collapses at 64 cores — a domain-decomposition artifact.**
   Core-count sweep (ns/day vs -nt), benchMEM:

   | cores | c6i | c7i | c8i |
   |-------|-----|-----|-----|
   | 32 | 61 | 72 | 97 |
   | 48 | 108 | 123 | **162** |
   | 64 | 81 | 82 | **104** ↓ |
   | 96 | 141 | 137 | 174 |

   Every chip is *slower at 64 cores than at 48* — GROMACS's 3D domain
   decomposition / PP-PME rank split factorizes badly at 64, falling back to a
   poor grid. The naive "more cores = faster" and "use the whole box" instincts
   both fail: 48 cores beats 64 on every generation, and the best core count is
   not the maximum.

*If you want CPU throughput: buy bandwidth, not the newest generation or the most
cores. Check the NUMA layout your cloud vendor actually gives you, and don't
assume filling the box is optimal.* (Phase 6)

**Data quality:** @96c and @48c are 3-replicate mean±CI (the load-bearing cells).
The 64-core-collapse and per-core-scaling numbers in the tables are rep0 (the
effects there are far larger than the ~2-5% CIs seen at 48/96, so the direction
is robust; exact values pending full CIs). c5's 64/96-core points are still
landing (oldest chip = slowest cells). The 48-core staircase (c6i 108.5±3.0 <
c7i 123.3±0.9 < c8i 161.4±3.7) is CI-clean and is the true per-core ordering.

---

## F1 — For a small system, the CPU beats the GPU on cost, throughput, AND wait (D3)

**System:** benchMEM (82k atoms, membrane protein — a *small* MD system).

| arm | config | ns/day (±95% CI) | $/hr OD | ns/$ OD | GPU SM-active | capacity |
|-----|--------|------------------|---------|---------|---------------|----------|
| **c8a** (AMD Zen5, CPU) | cpu-base | **270.6 ± 2.9** | 5.17 | **2.18** | — | immediate |
| c8g (Graviton4, CPU) | cpu-base | 117.6 ± 0.3 | 3.83 | 1.28 | — | immediate |
| g6 (NVIDIA L4, GPU) | gpu-prod | 182.3 ± 1.5 | 1.32 | 2.30 | **19% mean** | **InsufficientInstanceCapacity** |

**The GPU loses on every axis that matters for this system:**

1. **Throughput (wall-clock ns/day).** The AMD CPU (270.6) is **33% faster** than
   the L4 GPU (182.3). The intuition "GPU = faster" is simply false here — a small
   system cannot feed the card.

2. **Utilization.** DCGM (not nvidia-smi) measures **19% mean SM-active, 0.2%
   DRAM-active** on the L4: the card is ~80% idle running benchMEM. You pay for
   silicon you don't use. `nvidia-smi utilization.gpu` would have reported ~100%
   and hidden this entirely — the metric choice is the finding's foundation.

3. **Cost (ns/$).** The GPU's ns/$ (2.30) barely edges the best CPU (2.18), and
   that thin edge evaporates once utilization and wait are considered.

4. **Wait / availability (time/result).** g6 returned
   `InsufficientInstanceCapacity` in us-east-1 during this campaign — the GPU
   result was unobtainable *at any wall-clock time*, while the CPU launched
   immediately. `time/result = wait + runtime`: for the GPU that's `∞ + slower`;
   for the CPU it's `~0 + faster`. This is the cloud's queue (the direct analogue
   of an on-prem scheduler wait), and it decides the comparison.

**Conclusion.** For a system too small to fill the card, the rational choice is
the CPU that is cheaper-or-equal per result, *faster* in wall clock, fully
utilized, and always available. The batch-model instinct ("get on the GPU") is
wrong here on cost, throughput, and time-to-result simultaneously. The GPU only
earns its premium on systems large enough to saturate it — which is why SM-active
utilization, not ns/day, is the discriminator, and why benchRIB (2M atoms) is the
GPU-favorable contrast still to be measured.

*If you want a small system done cheaply and now: use the CPU.* (D3)

**Caveats / regime boundary:** benchMEM's distributed `.tpr` uses all-bonds
constraints, so the fully GPU-resident update path (`-update gpu`) is unsupported
(recorded `infeasible:fit`); `gpu-prod` (`-update cpu`) is the runnable GPU config
and what's reported above. GPU capacity is intermittent — the finding reflects a
real snapshot, not a permanent quota.
