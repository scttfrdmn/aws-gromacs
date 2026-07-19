"""Thin wrappers around the spore.host tools (truffle / spawn).

CLI syntax verified against truffle/spawn v-installed on 2026-07-18 via `--help`
and (for pricing, a read-only call) live JSON output. Where a call could only be
confirmed by actually launching an instance -- which spends money -- the
remaining assumption is still marked `# SPORE:` and noted in the docstring.

Model (differs from the original guesses):
  * There is no `truffle price`. Pricing comes from `truffle spot <type> -o json`,
    which returns a per-AZ array with `spot_price` and `on_demand_price`.
  * `spawn` launches an EC2 instance BY NAME from an AMI (`--ami`), not a
    container. It is addressed by that name thereafter (connect/terminate), so we
    use the name as the handle rather than parsing an id out of launch JSON.
  * Remote exec is `spawn connect <name> -- <cmd>`; there is no `spawn exec`.
  * Teardown is `spawn terminate <name> --yes`; there is no `spawn down`.

Every function honors DRY_RUN, which prints the command instead of running it.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def _run(cmd: list[str], capture: bool = True) -> str:
    printable = " ".join(shlex.quote(c) for c in cmd)
    if DRY_RUN:
        print(f"[dry-run] {printable}")
        return ""
    return subprocess.run(cmd, check=True, capture_output=capture, text=True).stdout


@dataclass
class Prices:
    on_demand_hr: float
    spot_hr: float           # cheapest spot across AZs in the region


def truffle_price(instance_type: str, region: str) -> Prices:
    """On-demand and current best (cheapest) spot $/hr for an instance type.

    Verified: `truffle spot <type> --regions <r> --show-savings -o json` emits a
    JSON array, one object per AZ, with float `spot_price` and `on_demand_price`.
    On-demand is constant across AZs; spot is per-AZ, so we take the minimum
    (the ns_per_dollar_spot column is a best-case-spot figure by construction).
    """
    out = _run(["truffle", "spot", instance_type, "--regions", region,
                "--show-savings", "-o", "json"])
    if DRY_RUN:
        return Prices(on_demand_hr=1.0, spot_hr=0.4)
    rows = json.loads(out)
    if not rows:
        raise RuntimeError(f"no spot pricing for {instance_type} in {region}")
    on_demand = float(rows[0]["on_demand_price"])
    spot = min(float(r["spot_price"]) for r in rows)
    return Prices(on_demand_hr=on_demand, spot_hr=spot)


# GROMACS is delivered as a container (docs/gromacs-delivery.md), and each cell
# runs AUTONOMOUSLY: spawn launches a bare AL2023 instance whose --command is
# cell_runner.sh, which installs docker, pulls the arch image, runs the wrapper,
# writes logs + timing.json to S3, and self-terminates. The coordinator never
# holds an SSH session -- it launches and polls S3. This replaced the earlier
# `spawn connect`-per-cell model, whose long-lived concurrent SSH sessions
# deadlocked the parallel batch (only the autonomous build instances ran clean).
HOST_WORK = "/tmp/bench"
COMPLETION_FILE = f"{HOST_WORK}/SPAWN_COMPLETE"   # host path spawn watches
RUNNER_URL = "https://github.com/scttfrdmn/aws-gromacs/raw/main/cell_runner.sh"
WRAPPER_URL = "https://github.com/scttfrdmn/aws-gromacs/raw/main/mdrun_wrapper.sh"


def launch_cell(instance_type: str, image: str, env: dict[str, str], gpu: bool,
                ttl_minutes: int, idle_minutes: int, region: str, name: str,
                results_s3: str, iam_policy_file: str | None = None,
                ami: str | None = None) -> str:
    """Launch one autonomous benchmark cell. Fire-and-forget: returns as soon as
    spawn accepts the launch (no --wait-for-ssh), because the whole job runs from
    cell_runner.sh via --command and reports to S3. Returns the instance name.

    Teardown guarantees (no run can strand a billing instance):
      * completion file   cell_runner.sh touches it on success -> --on-complete
                          terminate (default; bounded cost).
      * `--terminate-on-error`  reaps if spored/bootstrap fails.
      * `--ttl` / `--idle-timeout`  backstops.

    The --command curls cell_runner.sh from the public repo and runs it with the
    cell's env inlined (image, region, gpu, results prefix, and the wrapper's
    TPR/NSTEPS/... vars). On-demand only (timing integrity).

    AMI: CPU cells auto-detect (AL2023). GPU cells MUST pass `ami` -- spawn's
    auto-detect fails for GPU instance types (ParameterNotFound; filed
    spore-host/spawn#384), and GROMACS-GPU needs NVIDIA drivers on the host, so
    we pin a Deep Learning Base OSS Nvidia AMI via matrix.yaml `gpu_ami`.
    """
    exports = {
        "IMAGE": image, "AWS_REGION": region, "GPU": "1" if gpu else "0",
        "RESULTS_S3": results_s3, "WRAPPER_URL": WRAPPER_URL,
        "COMPLETION_FILE": COMPLETION_FILE,
        **env,   # TPR_SRC, NSTEPS, MDRUN_FLAGS, MIG_*, MPS_PROCS, REPLICATES
    }
    export_line = " ".join(f"{k}={shlex.quote(v)}" for k, v in exports.items())
    command = (f"export {export_line}; "
               f"curl -fsSL {shlex.quote(RUNNER_URL)} | bash")
    cmd = ["spawn", "launch", name,
           "--instance-type", instance_type,
           "--region", region,
           "--ttl", f"{ttl_minutes}m",
           "--idle-timeout", f"{idle_minutes}m",
           "--completion-file", COMPLETION_FILE,
           "--on-complete", "terminate",
           "--terminate-on-error",
           "--command", command,
           "-o", "json"]
    if ami:
        cmd += ["--ami", ami]
    if iam_policy_file:
        # Instance role: ECR pull + S3 read/write on the bench bucket.
        cmd += ["--iam-policy-file", iam_policy_file]
    # Retry transient launch failures: concurrent launches can still race on
    # shared-infra creation (IAM role, VPC/SG) or hit API throttling even with a
    # stagger. A couple of backoff retries makes the batch robust. DRY_RUN and
    # hard errors (bad AMI, quota) surface on the final attempt.
    if DRY_RUN:
        _run(cmd)
        return name
    last = None
    for attempt in range(3):
        try:
            _run(cmd)
            return name
        except subprocess.CalledProcessError as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise last



def fetch_s3(s3_prefix: str, local_path: str) -> None:
    """Pull a cell's logs from S3 (where the wrapper pushed them before the
    completion sentinel). Race-free vs. teardown, and works even if the instance
    is already gone. Uses the local awscli + configured profile/creds.
    """
    if DRY_RUN:
        print(f"[dry-run] aws s3 cp {s3_prefix}/ {local_path} --recursive")
        return
    os.makedirs(local_path, exist_ok=True)
    subprocess.run(["aws", "s3", "cp", f"{s3_prefix}/", local_path,
                    "--recursive", "--only-show-errors"], check=True)


def terminate(handle: str) -> None:
    """Tear down explicitly. TTL/idle will also reap it, but do it on
    success/failure regardless. Verified via `spawn terminate --help`."""
    _run(["spawn", "terminate", handle, "--yes"])
