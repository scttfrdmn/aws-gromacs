"""Extract ns/day from GROMACS md.log file(s), with replicate statistics.

GROMACS ends a run with:
    Performance:       42.123        0.570
where the first number is ns/day.

Two levels of aggregation, do not conflate them:

  1. Within one replicate, throughput is the SUM across concurrent logs
     (one per MIG slice / MPS proc; a whole-card run is a single log).
  2. Across replicates, throughput is a DISTRIBUTION. ns/day is noisy run to
     run -- thermal throttling, noisy neighbours, PME auto-tuning draw, DVFS,
     spot placement -- so a single number is not a measurement. We report the
     mean and a 95% confidence interval over independent timed runs.

This is a confidence interval on the *performance proxy*. It is NOT scientific
seed replication for a converged observable -- that is a downstream concern
(PLAN-cost-per-result.md) and deliberately not computed here.

Replicates are tagged in the log filename as `rep<N>_...` by mdrun_wrapper.sh.
Files with no rep tag are treated as a single replicate (back-compat).
"""
from __future__ import annotations

import glob
import math
import os
import re
import sys

_PERF = re.compile(r"^\s*Performance:\s+([0-9.eE+-]+)")
_REP = re.compile(r"rep(\d+)")

# Two-sided 95% Student-t critical values by degrees of freedom. Small-n CIs
# need the t-distribution, not 1.96 -- with 3 replicates the difference is ~60%.
# No scipy dependency: table for df 1..30, normal limit beyond.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def _t95(df: int) -> float:
    return _T95.get(df, 1.960)


def ns_day_from_file(path: str) -> float:
    with open(path) as fh:
        for line in fh:
            m = _PERF.match(line)
            if m:
                return float(m.group(1))
    raise ValueError(f"no Performance line in {path}")


def replicate_ns_days(pattern: str) -> list[float]:
    """One aggregate ns/day per replicate. Files are grouped by their `rep<N>`
    tag; within a group throughput is summed across concurrent logs (MIG/MPS
    slices). Untagged files collapse to a single replicate."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(pattern)
    groups: dict[str, float] = {}
    for f in files:
        m = _REP.search(os.path.basename(f))
        key = m.group(1) if m else "0"
        groups[key] = groups.get(key, 0.0) + ns_day_from_file(f)
    return [groups[k] for k in sorted(groups)]


def ns_day_stats(pattern: str) -> dict:
    """Mean ns/day and a 95% CI over replicates.

    Returns: n, mean, std (sample), sem, ci95 (half-width), rel_ci (ci95/mean).
    With n<2 the CI is undefined and reported as 0.0 -- a single-replicate cell
    is flagged by n=1, not silently dressed up with error bars it doesn't have.
    """
    vals = replicate_ns_days(pattern)
    n = len(vals)
    mean = sum(vals) / n
    if n < 2:
        return {"n": n, "mean": mean, "std": 0.0, "sem": 0.0,
                "ci95": 0.0, "rel_ci": 0.0}
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = math.sqrt(var)
    sem = std / math.sqrt(n)
    ci95 = _t95(n - 1) * sem
    return {"n": n, "mean": mean, "std": std, "sem": sem,
            "ci95": ci95, "rel_ci": (ci95 / mean if mean else 0.0)}


# Back-compat: sum across all matching logs, ignoring replicate structure.
def ns_day_from_glob(pattern: str) -> float:
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(pattern)
    return sum(ns_day_from_file(f) for f in files)


if __name__ == "__main__":
    # Usage: parse_log.py "results/g7e-mig4/logs/md*.log"
    s = ns_day_stats(sys.argv[1])
    print(f"ns/day mean={s['mean']:.3f}  95% CI +/-{s['ci95']:.3f} "
          f"({100 * s['rel_ci']:.1f}%)  n={s['n']}")
