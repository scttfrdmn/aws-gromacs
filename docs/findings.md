# Findings

Measured results as they land. Each is a real number from the harness (≥3 timed
replicates, 95% CI), not an assertion. Raw data: `results/` + S3
`gromacs-bench/results/`. This file is the running record behind the eventual
decision table (`PLAN-cost-per-result.md`).

Region: us-east-1. On-demand pricing (timing runs are on-demand; spot enters
only via the ns/$ spot column). GROMACS 2025.2.

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

   **On the LARGE system (benchRIB, 2M atoms, 96 cores) the bandwidth inversion
   is unambiguous** — CIs razor-tight and non-overlapping:
   c7i **8.91 ± 0.02** < c6i **9.62 ± 0.01** < c8i **12.04 ± 0.10** ns/day.
   Ordering follows STREAM bandwidth exactly (186 < 272 < 355), NOT generation
   number: the 2023 c7i is genuinely, measurably slower than the 2021 c6i on a
   memory-bound 2M-atom system, because AWS gives c7i.24xlarge less bandwidth.
   Where the small system left c6i/c7i within noise, the large system — which
   truly saturates memory — separates them cleanly. (c5 large: ~6.08 ns/day, rep0
   only — it was so slow on 2M atoms it hit the idle/TTL window before finishing
   3 replicates: old gen can be operationally unviable for large work, not just
   slower-per-dollar.)

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
