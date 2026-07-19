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

| gen | µarch (year) | $/hr OD | STREAM GB/s | NUMA | ns/day @96c |
|-----|--------------|---------|-------------|------|-------------|
| c5  | Cascade Lake (2019) | 4.08 | 161 | 2 | 80 (partial) |
| c6i | Ice Lake (2021)     | 4.08 | 272 | 2 | 141 |
| c7i | Sapphire Rapids (2023) | 4.28 | 186 | **1** | 137 |
| c8i | Granite Rapids (2025) | 4.50 | 355 | 2 | 174 |

**Three measured findings:**

1. **Throughput tracks memory bandwidth, not generation or price.** ns/day rises
   with STREAM GB/s (161→272→355 → 80→141→174), not with generation number or
   $/hr (which rise only ~10% c5→c8i). MD on CPU is bandwidth-bound; the menu
   number ($/hr) and the spec-sheet generation both fail to predict $/result.

2. **The generational order INVERTS at full width — and bandwidth explains it.**
   At 96 cores c7i (137) sits *below* c6i (141), breaking the c5<c6i<c7i<c8i
   staircase. Cause: AWS provisions c7i.24xlarge as a **single NUMA node** with
   the lowest bandwidth of the modern three (186 GB/s, barely above 2019's c5).
   The core-count sweep confirms it: at 8/16/32/48 cores c7i is *faster* than c6i
   (true per-core IPC), but its bandwidth ceiling drops it below c6i once the
   cores are all fighting for memory. The full-width number lies; the curve
   tells the truth.

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

**Caveat:** rep0 values shown; 3-replicate CIs to be folded in. c5 high-core
points still landing (oldest chip, slowest cells). The 48-core monotonic
staircase (c5 92 < c6i 108 < c7i 123 < c8i 162) is the clean per-core ordering.

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
