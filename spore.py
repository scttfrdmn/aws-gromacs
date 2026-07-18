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


COMPLETION_FILE = "/tmp/SPAWN_COMPLETE"


def spawn(instance_type: str, image: str, ttl_minutes: int, idle_minutes: int,
          region: str, name: str, on_complete: str = "terminate",
          pre_stop: str | None = None) -> str:
    """Launch an on-demand instance with auto-termination. Returns the name,
    which is the handle spawn uses for connect/terminate.

    Three independent teardown guarantees, so no run can strand a billing
    instance:
      * `--ttl`           hard upper bound on lifetime
      * `--idle-timeout`  reap if nothing is running
      * completion file   mdrun_wrapper.sh writes /tmp/SPAWN_COMPLETE when the
                          replicates finish; spawn then runs `--on-complete`
                          (default `terminate` -- bounded cost; `stop` keeps
                          billing EBS, per spawn's own warning).
    `--terminate-on-error` also reaps the instance if spored never comes up.
    Optional `--pre-stop` runs on the box before teardown (e.g. an
    `aws s3 sync` of results), which is the robust alternative to fetch().

    `image` is an AMI id (ami-...) or 'auto'; timing runs are on-demand (no
    --spot) on purpose. Syntax verified via `spawn launch --help`.
    # SPORE: not yet confirmed end-to-end against a live launch (would spend).
    Delivering GROMACS is a Phase-0 decision (custom AMI vs. --command container
    pull); `image` maps to --ami here -- see issue #2 / matrix.yaml `images:`.
    """
    cmd = ["spawn", "launch", name,
           "--instance-type", instance_type,
           "--region", region,
           "--ttl", f"{ttl_minutes}m",
           "--idle-timeout", f"{idle_minutes}m",
           "--completion-file", COMPLETION_FILE,
           "--on-complete", on_complete,
           "--terminate-on-error",
           "--wait-for-ssh",
           "-o", "json"]
    if image and image != "auto":
        cmd += ["--ami", image]
    if pre_stop:
        cmd += ["--pre-stop", pre_stop]
    _run(cmd)
    # Addressed by name from here on; no id parsing needed.
    return name


def run_remote(handle: str, command: str, env: dict[str, str] | None = None) -> str:
    """Execute a shell command on the instance, return its stdout.

    Verified: `spawn connect <name> -- <cmd>...` runs a command over SSH.
    # SPORE: end-to-end (env propagation, exit-code passthrough) unconfirmed
    until the Phase-1 live cell.
    """
    env = env or {}
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    full = f"{env_prefix} {command}".strip()
    return _run(["spawn", "connect", handle, "--", "bash", "-lc", full])


def fetch(handle: str, remote_path: str, local_path: str) -> None:
    """Copy file(s) off the instance.

    spawn has no `cp`/`scp` subcommand, so we stream a tar of the matched files
    through `spawn connect` and unpack locally. `remote_path` may be a glob.
    # SPORE: unconfirmed until the Phase-1 live cell. If this proves flaky, the
    # robust alternative is to have mdrun_wrapper.sh push logs to S3 and fetch
    # from there (the instance already has awscli + the configured bucket).
    """
    remote = f"tar czf - {remote_path} 2>/dev/null || true"
    if DRY_RUN:
        print(f"[dry-run] spawn connect {handle} -- bash -lc {shlex.quote(remote)} "
              f"| tar xzf - -C {local_path}")
        return
    os.makedirs(local_path, exist_ok=True)
    proc = subprocess.run(["spawn", "connect", handle, "--", "bash", "-lc", remote],
                          check=True, capture_output=True)
    subprocess.run(["tar", "xzf", "-", "-C", local_path],
                   input=proc.stdout, check=True)


def terminate(handle: str) -> None:
    """Tear down explicitly. TTL/idle will also reap it, but do it on
    success/failure regardless. Verified via `spawn terminate --help`."""
    _run(["spawn", "terminate", handle, "--yes"])
