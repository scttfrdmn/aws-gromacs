#!/usr/bin/env python3
"""Rebuild results.csv from the per-cell logs durably stored in S3.

The benchmark's source of truth is the md_rep*.log files each cell pushes to
s3://<bucket>/gromacs-bench/results/<cell>/logs/ before signalling completion.
The local results.csv is a derived aggregate -- if a run is interrupted (or a
later run clobbers it), this rebuilds the ns/day + CI columns from those logs and
re-derives ns/$ from current truffle pricing.

Does NOT reconstruct wait/runtime timings (acquire_s/provision_s/...): those are
observed live by the coordinator and not stored in the logs. Rebuilt rows carry
the performance spine (ns/day, CI, ns/$); timing columns are left blank. Use for
recovering the price-performance table, not the scheduling-model measurement.

Usage:
  AWS_PROFILE=... python reconstruct_from_s3.py --bucket <b> --region <r> \
      --cells c8a-cpu-base-small,...   (or --auto to list from S3)
"""
from __future__ import annotations

import argparse
import csv
import pathlib

import spore
from parse_log import ns_day_stats

RESULTS = pathlib.Path("results")


def ns_per_dollar(ns_day: float, price_hr: float) -> float:
    return round(ns_day / (price_hr * 24.0), 2) if price_hr > 0 else 0.0


def cell_meta(cell: str) -> tuple[str, str, str]:
    """Split a cell dir name into (instance, config, workload). Names are
    '<instance>-<config>-<workload>'; workload is the last token, instance the
    first, config the middle (which may itself contain hyphens, e.g. cpu-base)."""
    parts = cell.split("-")
    workload = parts[-1]
    # instance ids in the matrix: c8i, c8a, c8a-avx2, c8g, g6, g6e, g7e
    inst = parts[0]
    if len(parts) >= 2 and parts[1] == "avx2":   # c8a-avx2
        inst = f"{parts[0]}-{parts[1]}"
    config = "-".join(parts[len(inst.split('-')):-1])
    return inst, config, workload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--cells", default="", help="comma list of cell dir names")
    ap.add_argument("--out", default=str(RESULTS / "results.csv"))
    args = ap.parse_args()

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    if not cells:
        raise SystemExit("pass --cells a,b,c (cell dir names under results/)")

    # Pricing is per instance type; cache so we hit truffle once per type.
    price_cache: dict[str, spore.Prices] = {}
    type_for = {"c8i": "c8i.24xlarge", "c8a": "c8a.24xlarge",
                "c8a-avx2": "c8a.24xlarge", "c8g": "c8g.24xlarge",
                "g6": "g6.4xlarge", "g6e": "g6e.2xlarge", "g7e": "g7e.2xlarge"}

    rows = []
    for cell in cells:
        inst, config, workload = cell_meta(cell)
        logs = RESULTS / cell / "logs"
        pattern = str(logs / "md*.log")
        try:
            stats = ns_day_stats(pattern)
        except FileNotFoundError:
            print(f"skip {cell}: no logs at {pattern}")
            continue
        itype = type_for.get(inst, f"{inst}.unknown")
        if itype not in price_cache:
            price_cache[itype] = spore.truffle_price(itype, args.region)
        pr = price_cache[itype]
        total = stats["mean"]
        od, spot = pr.on_demand_hr, pr.spot_hr
        rows.append({
            "workload": workload, "instance": inst, "config": config,
            "replicates": stats["n"],
            "ns_day_total": round(total, 3),
            "ns_day_ci95": round(stats["ci95"], 3),
            "ns_day_rel_ci": round(stats["rel_ci"], 4),
            "od_hr": od, "spot_hr": spot,
            "ns_per_dollar_od": ns_per_dollar(total, od),
            "ns_per_dollar_spot": ns_per_dollar(total, spot),
        })
        print(f"OK {cell}: {total:.2f} ns/day +/- {stats['ci95']:.2f} "
              f"(n={stats['n']}) -> {ns_per_dollar(total, od)} ns/$od")

    fields = ["workload", "instance", "config", "replicates", "ns_day_total",
              "ns_day_ci95", "ns_day_rel_ci", "od_hr", "spot_hr",
              "ns_per_dollar_od", "ns_per_dollar_spot"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
