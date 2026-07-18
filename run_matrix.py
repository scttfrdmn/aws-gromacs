#!/usr/bin/env python3
"""Coordinator for the GROMACS price-performance sweep.

A cell is (workload x instance x config). Configs are the methodology axis:
timestep/HMR, force placement, MIG carve, MPS packing. Local hardware is a
first-class instance provider alongside AWS.

  DRY_RUN=1 python run_matrix.py --dry-run      # print the plan, run nothing
  python run_matrix.py --list                   # show cells, no execution
  python run_matrix.py --phase1                 # one cell, validate teardown
  python run_matrix.py --tier1                  # D1/D2/D3/D13 config sweep
  python run_matrix.py                          # everything

Emits results/results.csv with ns/day and $/ns. NOT $/result -- ns/day is not a
result. Derive $/result downstream once a convergence criterion exists.
"""
from __future__ import annotations

import argparse
import csv
import os
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

import providers
import spore
from parse_log import ns_day_stats

RESULTS = pathlib.Path("results")
CSV_PATH = RESULTS / "results.csv"
# Instance role for benchmark cells: ECR pull + S3 read/write on the bench
# bucket (the in-container `aws s3 cp` of the tpr needs it; the auto AL2023 AMI
# ships no instance role by default).
IAM_POLICY = str(pathlib.Path(__file__).parent / "build" / "benchmark-instance-policy.json")
FIELDS = ["workload", "atoms", "instance", "class", "config", "provider",
          "outcome", "reason", "sims",
          # ns/day is a distribution over replicates, not a scalar. Report the
          # mean and its spread so the ns/$ spine carries a confidence interval.
          "replicates", "ns_day_total", "ns_day_per_sim",
          "ns_day_std", "ns_day_ci95", "ns_day_rel_ci",
          "runtime_s", "acquire_s", "capacity_seen_s", "acquire_method",
          "provision_s",
          "wait_s", "wait_s_best", "wait_s_p50", "wait_s_p90",
          "time_to_result_s", "time_to_result_best_s",
          "od_hr", "spot_hr", "ns_per_dollar_od", "ns_per_dollar_spot"]


def blank_row(wl, inst, cf, outcome, reason):
    """An infeasible cell is a row, not a gap. 'Could not run it at all' is a
    result -- and the typed reason is the argument."""
    r = dict.fromkeys(FIELDS, "")
    r.update(workload=wl["id"], atoms=wl["atoms"], instance=inst["id"],
             class_=inst["class"], config=cf["id"],
             provider=inst.get("provider", "aws"),
             outcome=outcome, reason=reason)
    r["class"] = inst["class"]
    r.pop("class_", None)
    return r


def infeasible_for(rules, wl, inst, cf):
    for r in rules or []:
        if all([r.get("workload", wl["id"]) == wl["id"],
                r.get("instance", inst["id"]) == inst["id"],
                r.get("config", cf["id"]) == cf["id"]]):
            return f"infeasible:{r.get('class','unspecified')}", r.get("reason", "")
    return None


def applies(cfg_entry: dict, inst: dict) -> bool:
    targets = cfg_entry.get("applies_to", [])
    return inst["class"] in targets or inst["id"] in targets


def skipped(skip_rules: list[dict], wl: dict, inst: dict, cf: dict) -> bool:
    for r in skip_rules or []:
        if all([
            r.get("workload", wl["id"]) == wl["id"],
            r.get("instance", inst["id"]) == inst["id"],
            r.get("config", cf["id"]) == cf["id"],
        ]):
            return True
    return False


def build_cells(cfg: dict, tier1: bool = False) -> list[tuple[dict, dict, dict]]:
    tier1_ids = {"gpu-resident", "gpu-cpu-pme", "hmr", "cpu-base", "cpu-hmr",
                 "mig2", "mig4", "mps4"}
    cells = []
    for wl in cfg["workloads"]:
        for inst in cfg["instances"]:
            for cf in cfg["configs"]:
                if not applies(cf, inst):
                    continue
                if tier1 and cf["id"] not in tier1_ids:
                    continue
                if skipped(cfg.get("skip"), wl, inst, cf):
                    continue
                cells.append((wl, inst, cf))
    return cells


def results_s3(cfg: dict, name: str) -> str:
    """S3 prefix a cell pushes its logs to before signalling completion."""
    return f"s3://{cfg['s3_bucket']}/gromacs-bench/results/{name}/logs"


def env_for(cfg: dict, wl: dict, cf: dict, local: bool, name: str) -> dict[str, str]:
    tpr = f"{wl['tpr']}{cf.get('tpr_variant','')}.tpr"
    src = (f"{os.environ.get('LOCAL_TPR_DIR','./tpr')}/{tpr}" if local
           else f"s3://{cfg['s3_bucket']}/gromacs-bench/tpr/{tpr}")
    env = {
        "TPR_SRC": src,
        "NSTEPS": str(cfg["nsteps"]),
        "MDRUN_FLAGS": cf.get("mdrun_flags", ""),
        "MIG_SLICES": str(cf.get("mig_slices", 0)),
        "MIG_PROFILE": cf.get("mig_profile", ""),
        "MPS_PROCS": str(cf.get("mps_procs", 0)),
        # Timed replicates for the ns/day CI. Config may override the global.
        "REPLICATES": str(cf.get("replicates", cfg.get("replicates", 3))),
    }
    if not local:
        # Push logs to S3 before the completion sentinel, so results are durable
        # before spawn's --on-complete teardown can race the coordinator's fetch.
        env["RESULTS_S3"] = results_s3(cfg, name)
    return env


def _expand_env(obj):
    """Expand ${VAR} in every string in the loaded config, so account- and
    region-specific values (bucket, ECR URIs) stay out of the committed file --
    the repo is public and account-agnostic. Unset vars raise, rather than
    silently leaving a literal ${...} that would fail obscurely at launch.
    DRY_RUN skips the check so `--list`/dry sweeps work with nothing exported."""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        if spore.DRY_RUN:
            # Leave placeholders visible in dry-run output; don't require env.
            return os.path.expandvars(obj)
        missing = [m for m in re.findall(r"\$\{(\w+)\}", obj) if m not in os.environ]
        if missing:
            raise SystemExit(f"config references unset env var(s): {', '.join(missing)}"
                             f" (in {obj!r})")
        return os.path.expandvars(obj)
    return obj


def ns_per_dollar(ns_day: float, price_hr: float) -> float:
    return ns_day / (price_hr * 24.0) if price_hr > 0 else 0.0


def run_cell(cfg: dict, wl: dict, inst: dict, cf: dict,
             queue_samples: dict | None = None) -> dict:
    name = f"{inst['id']}-{cf['id']}-{wl['id']}"
    cell_dir = RESULTS / name
    (cell_dir / "logs").mkdir(parents=True, exist_ok=True)
    prov = inst.get("provider", "aws")
    env = env_for(cfg, wl, cf, prov != "aws", name)

    if prov == "local":
        od = spot = float(inst.get("amortized_hr", 0.0))
        t = providers.local_run(env, str(cell_dir))

    elif prov == "onprem":
        od = spot = float(inst.get("amortized_hr", 0.0))
        t = providers.onprem_wait_and_run(inst, env, str(cell_dir))
        t.acquire_s, t.provision_s = t.wait_s, 0.0
        # Fold in the independently probed queue distribution, so wait is
        # reported as best/p50/p90 rather than a single lucky or unlucky draw.
        samples = (queue_samples or {}).get(inst["id"], [])
        if samples:
            t.wait_samples = samples + [t.wait_s]
            t.summarize()

    else:  # aws via spore
        prices = spore.truffle_price(inst["type"], cfg["region"])
        od, spot = prices.on_demand_hr, prices.spot_hr
        max_wait = float(cfg.get("capacity_max_wait_minutes", 30)) * 60
        image = cfg["images"][inst["arch"]]
        gpu = inst["class"] == "gpu"
        if cfg.get("use_lagotto", True):
            # Watch for capacity rather than discovering it by failing.
            handle, acquire_s, seen_s = providers.lagotto_acquire(
                inst["type"], cfg["region"], max_wait,
                cfg["ttl_minutes"], cfg["idle_minutes"], name,
                iam_policy_file=IAM_POLICY)
            method = "lagotto"
        else:
            handle, acquire_s = providers.cloud_acquire(
                lambda: spore.spawn(inst["type"], cfg["ttl_minutes"],
                                    cfg["idle_minutes"], cfg["region"], name,
                                    iam_policy_file=IAM_POLICY),
                max_wait_s=max_wait)
            seen_s, method = acquire_s, "retry"
        granted = time.time()
        try:
            # provision = boot + runtime install + ECR login + pull + stage.
            # Timed as provision_s, kept out of runtime_s so the split stays honest.
            spore.pull(handle, image, cfg["region"], gpu=gpu)
            ready = time.time()
            # runtime = the docker run of the wrapper (the timed GROMACS work).
            spore.run_container(handle, image, env, gpu)
            done = time.time()
            # Results were pushed to S3 by the wrapper before the sentinel, so
            # read them from there -- durable even if --on-complete already
            # terminated the box (avoids the fetch-vs-teardown race).
            spore.fetch_s3(results_s3(cfg, name), str(cell_dir / "logs") + "/")
        finally:
            spore.terminate(handle)
        # Cloud queues too: acquire_s is contended-capacity wait, the direct
        # analogue of scheduler queue delay. provision_s is boot + pull + stage.
        t = providers.Timing(runtime_s=done - ready,
                             acquire_s=acquire_s,
                             capacity_seen_s=seen_s,
                             acquire_method=method,
                             provision_s=ready - granted)
        samples = (queue_samples or {}).get(inst["id"], [])
        t.wait_samples = (samples or []) + [acquire_s + t.provision_s]
        t.summarize()

    sims = max(cf.get("mig_slices", 0), cf.get("mps_procs", 0), 1)
    # ns_day_stats sums slices within each replicate, then treats replicates as
    # a distribution: total is the mean per-replicate throughput, with a CI.
    if spore.DRY_RUN:
        stats = {"n": 0, "mean": 0.0, "std": 0.0, "ci95": 0.0, "rel_ci": 0.0}
    else:
        stats = ns_day_stats(str(cell_dir / "logs" / "md*.log"))
    total = stats["mean"]

    return {
        "workload": wl["id"], "atoms": wl["atoms"],
        "instance": inst["id"], "class": inst["class"],
        "config": cf["id"], "provider": prov,
        "outcome": "ran", "reason": "",
        "sims": sims,
        "replicates": stats["n"],
        "ns_day_total": round(total, 3),
        "ns_day_per_sim": round(total / sims, 3),
        "ns_day_std": round(stats["std"], 3),
        "ns_day_ci95": round(stats["ci95"], 3),
        "ns_day_rel_ci": round(stats["rel_ci"], 4),
        "runtime_s": round(t.runtime_s, 1),
        "acquire_s": round(t.acquire_s, 1),
        "capacity_seen_s": round(t.capacity_seen_s, 1),
        "acquire_method": t.acquire_method,
        "provision_s": round(t.provision_s, 1),
        "wait_s": round(t.wait_s, 1),
        "wait_s_best": round(t.wait_s_best, 1),
        "wait_s_p50": round(t.wait_s_p50, 1),
        "wait_s_p90": round(t.wait_s_p90, 1),
        # time-to-result reported twice: observed, and with the most generous
        # queue assumption available. Give away the favorable framing.
        "time_to_result_s": round(t.runtime_s + t.wait_s, 1),
        "time_to_result_best_s": round(t.runtime_s + t.wait_s_best, 1),
        "od_hr": od, "spot_hr": spot,
        "ns_per_dollar_od": round(ns_per_dollar(total, od), 2),
        "ns_per_dollar_spot": round(ns_per_dollar(total, spot), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="matrix.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print cells and exit")
    ap.add_argument("--tier1", action="store_true",
                    help="config-axis sweep only (D1/D2/D3/D13)")
    ap.add_argument("--phase1", action="store_true",
                    help="single validation cell: small / c8g / cpu-base")
    ap.add_argument("--workloads", default="",
                    help="comma list of workload ids to include (default: all)")
    ap.add_argument("--instances", default="",
                    help="comma list of instance ids to include (default: all)")
    ap.add_argument("--configs", default="",
                    help="comma list of config ids to include (default: all)")
    ap.add_argument("--exclude-configs", default="",
                    help="comma list of config ids to exclude (e.g. hmr when no -hmr tpr is staged)")
    ap.add_argument("--max-parallel", type=int, default=8,
                    help="max cells running concurrently (each on its own instance). "
                         "Elasticity is the point -- cells are independent, on dedicated "
                         "instances, so parallel execution does not affect timing. Capped "
                         "to bound quota use + teardown blast radius. 1 = sequential.")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the capacity-probe pre-pass (the optional wait-distribution "
                         "sampling). Use when you just want the ns/$ spine -- the retry-path "
                         "probe launches throwaway instances that cost money and can fail.")
    args = ap.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
        spore.DRY_RUN = True

    cfg = _expand_env(yaml.safe_load(open(args.config)))
    RESULTS.mkdir(exist_ok=True)

    if args.phase1:
        wl = next(w for w in cfg["workloads"] if w["id"] == "small")
        inst = next(i for i in cfg["instances"] if i["id"] == "c8g")
        cf = next(c for c in cfg["configs"] if c["id"] == "cpu-base")
        cells = [(wl, inst, cf)]
    else:
        cells = build_cells(cfg, tier1=args.tier1)
        # Optional subset filters -- run a real campaign over part of the matrix
        # (e.g. only the workloads/instances whose inputs are staged) without
        # faking rows for the rest. Additive: they only narrow build_cells().
        # An unknown id is a typo guard -- fail before spending, not silently.
        def _subset(flag: str, ids: set[str], kind: str) -> set[str] | None:
            if not flag:
                return None
            want = {s.strip() for s in flag.split(",") if s.strip()}
            unknown = want - ids
            if unknown:
                raise SystemExit(f"--{kind}: unknown id(s) {sorted(unknown)}; "
                                 f"known: {sorted(ids)}")
            return want
        cfg_ids_all = {c["id"] for c in cfg["configs"]}
        wl_ids = _subset(args.workloads, {w["id"] for w in cfg["workloads"]}, "workloads")
        in_ids = _subset(args.instances, {i["id"] for i in cfg["instances"]}, "instances")
        cf_ids = _subset(args.configs, cfg_ids_all, "configs")
        cf_excl = _subset(args.exclude_configs, cfg_ids_all, "exclude-configs")
        if wl_ids is not None:
            cells = [(w, i, c) for (w, i, c) in cells if w["id"] in wl_ids]
        if in_ids is not None:
            cells = [(w, i, c) for (w, i, c) in cells if i["id"] in in_ids]
        if cf_ids is not None:
            cells = [(w, i, c) for (w, i, c) in cells if c["id"] in cf_ids]
        if cf_excl is not None:
            cells = [(w, i, c) for (w, i, c) in cells if c["id"] not in cf_excl]

    if args.list:
        for wl, inst, cf in cells:
            print(f"{wl['id']:7s} {inst['id']:12s} {cf['id']}")
        print(f"\n{len(cells)} cells")
        return 0

    # Probe wait distributions once, up front, independently of the benchmark
    # runs -- capacity acquisition for cloud pools, queue delay for on-prem.
    # Only probe instances actually in this run's cells (never the whole matrix),
    # and skip entirely for --phase1: that is a single plumbing-validation cell,
    # where a wait *distribution* is meaningless and probing would spend on extra
    # (possibly GPU) launches the phase never intended.
    queue_samples: dict[str, list[float]] = {}
    max_wait = float(cfg.get("capacity_max_wait_minutes", 30)) * 60
    cell_instance_ids = {inst["id"] for _, inst, _ in cells}
    for inst in cfg["instances"]:
        if args.phase1 or args.no_probe or inst["id"] not in cell_instance_ids:
            continue
        prov = inst.get("provider", "aws")
        n = int(inst.get("wait_samples", inst.get("queue_samples", 0)))
        if not n:
            continue
        # The wait distribution is optional context, NOT the benchmark. A probe
        # failure must degrade to "no samples for this instance" (wait then falls
        # back to the single observed acquire), never abort the whole campaign
        # before a cell runs.
        try:
            if prov == "onprem":
                queue_samples[inst["id"]] = providers.onprem_probe_queue(inst, n)
            elif prov == "aws":
                if cfg.get("use_lagotto", True):
                    # Watching is free, so a distribution costs nothing.
                    queue_samples[inst["id"]] = providers.lagotto_probe(
                        inst["type"], cfg["region"], n, max_wait)
                else:
                    queue_samples[inst["id"]] = providers.cloud_probe_capacity(
                        inst["type"], cfg["region"], n, max_wait,
                        lambda i=inst: (lambda: spore.spawn(
                            i["type"], cfg["ttl_minutes"],
                            cfg["idle_minutes"], cfg["region"], f"probe-{i['id']}")))
        except Exception as e:
            print(f"WARN probe for {inst['id']} failed, continuing without its "
                  f"wait distribution: {str(e)[:120]}", file=sys.stderr)

    def run_one(wl: dict, inst: dict, cf: dict) -> dict:
        """Execute a single cell, returning its row. Each cell runs on its own
        dedicated instance, so this is safe to call concurrently -- and doing so
        is the thesis (elastic width), not a shortcut. ns/day comes from GROMACS's
        own timers on that box, unaffected by other in-flight cells."""
        try:
            row = run_cell(cfg, wl, inst, cf, queue_samples)
            noisy = row["replicates"] and row["replicates"] < 3
            wide = row["ns_day_rel_ci"] and row["ns_day_rel_ci"] > 0.05
            flag = " !thin" if noisy else (" !wide-CI" if wide else "")
            print(f"OK  {row['instance']:14s} {row['config']:12s} "
                  f"{row['workload']:7s} "
                  f"ns/day={row['ns_day_total']:>9}+/-{row['ns_day_ci95']:<7} "
                  f"n={row['replicates']} "
                  f"acq={row['acquire_s']:>6}s prov={row['provision_s']:>6}s "
                  f"ttr={row['time_to_result_s']:>8}s{flag}")
            return row
        except providers.CapacityUnavailable as e:
            # Cloud's own queue failing to deliver. Same outcome class as an
            # on-prem job that never starts.
            print(f"--  {inst['id']:14s} {cf['id']:12s} {wl['id']:7s} "
                  f"infeasible:capacity ({e})")
            return blank_row(wl, inst, cf, "infeasible:capacity", str(e)[:200])
        except Exception as e:
            print(f"ERR {inst['id']:14s} {cf['id']:12s} {wl['id']:7s} {e}",
                  file=sys.stderr)
            return blank_row(wl, inst, cf, "error", str(e)[:200])

    rows = []
    runnable = []
    for wl, inst, cf in cells:
        verdict = infeasible_for(cfg.get("infeasible"), wl, inst, cf)
        if verdict:
            # Infeasible cells launch nothing -- record synchronously, no worker.
            outcome, reason = verdict
            rows.append(blank_row(wl, inst, cf, outcome, reason))
            print(f"--  {inst['id']:14s} {cf['id']:12s} {wl['id']:7s} {outcome} ({reason})")
        else:
            runnable.append((wl, inst, cf))

    # Elastic width: run independent cells concurrently, capped so quota and the
    # teardown blast radius stay bounded. Each cell self-terminates on completion.
    workers = max(1, min(args.max_parallel, len(runnable)))
    if workers == 1:
        for wl, inst, cf in runnable:
            rows.append(run_one(wl, inst, cf))
    else:
        print(f"running {len(runnable)} cells, up to {workers} concurrent")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(run_one, wl, inst, cf) for wl, inst, cf in runnable]
            for fut in as_completed(futs):
                rows.append(fut.result())

    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {CSV_PATH} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
