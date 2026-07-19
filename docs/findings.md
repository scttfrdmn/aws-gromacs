# Findings

Measured results as they land. Each is a real number from the harness (≥3 timed
replicates, 95% CI), not an assertion. Raw data: `results/` + S3
`gromacs-bench/results/`. This file is the running record behind the eventual
decision table (`PLAN-cost-per-result.md`).

Region: us-east-1. On-demand pricing (timing runs are on-demand; spot enters
only via the ns/$ spot column). GROMACS 2025.2.

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
