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


# GROMACS is delivered as a container (docs/gromacs-delivery.md): spawn launches
# a bare AL2023 instance (auto-detected AMI, GPU variants already carry NVIDIA
# drivers), then we docker-pull + docker-run the arch-appropriate image. The work
# dir is bind-mounted host<->container so logs and the completion sentinel land
# where spawn (host) and fetch() can see them.
HOST_WORK = "/tmp/bench"
CTR_WORK = "/work"
COMPLETION_FILE = f"{HOST_WORK}/SPAWN_COMPLETE"   # host path spawn watches


def _registry(image: str) -> str:
    """ECR registry host from an image URI (everything before the first '/')."""
    return image.split("/", 1)[0]


def spawn(instance_type: str, ttl_minutes: int, idle_minutes: int,
          region: str, name: str, on_complete: str = "terminate",
          pre_stop: str | None = None, ami: str | None = None,
          iam_policy_file: str | None = None) -> str:
    """Launch a bare on-demand instance with auto-termination. Returns the name,
    which is the handle spawn uses for connect/terminate. GROMACS itself arrives
    later via pull()/run_container(); nothing GROMACS-specific is baked here.

    Three independent teardown guarantees, so no run can strand a billing
    instance:
      * `--ttl`           hard upper bound on lifetime
      * `--idle-timeout`  reap if nothing is running
      * completion file   mdrun_wrapper.sh (in the container) writes the sentinel
                          to the bind-mounted host path when replicates finish;
                          spawn then runs `--on-complete` (default `terminate` --
                          bounded cost; `stop` keeps billing EBS, per spawn).
    `--terminate-on-error` also reaps the instance if spored never comes up.
    Optional `--pre-stop` runs on the box before teardown (e.g. an
    `aws s3 sync` of results), the robust alternative/backstop to fetch().

    AMI is auto-detected (omit `--ami`); pass `ami` only to pin one. Timing runs
    are on-demand (no --spot) on purpose. Syntax verified via `spawn launch
    --help`.
    # SPORE: not yet confirmed end-to-end against a live launch (would spend).
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
    if ami:
        cmd += ["--ami", ami]
    if pre_stop:
        cmd += ["--pre-stop", pre_stop]
    if iam_policy_file:
        # Instance role: ECR pull + S3 read/write on the bench bucket, so the
        # in-container `aws s3 cp` (tpr) and the docker ECR pull both work.
        cmd += ["--iam-policy-file", iam_policy_file]
    _run(cmd)
    # Addressed by name from here on; no id parsing needed.
    return name


def ensure_runtime(handle: str, gpu: bool) -> None:
    """Install the container runtime on the bare AL2023 instance. Verified in
    Phase 1: the auto-detected AL2023 AMI does NOT ship Docker, so pull/run would
    fail without this. For GPU cells also install the nvidia-container-toolkit so
    `docker run --gpus all` works. Idempotent (dnf install is a no-op if present).
    Part of provisioning, so its wall-clock lands in provision_s.
    """
    steps = [
        "sudo dnf install -y -q docker",
        "sudo systemctl enable --now docker",
    ]
    if gpu:
        # nvidia-container-toolkit repo + install, then wire it into docker.
        steps += [
            "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo "
            "| sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null",
            "sudo dnf install -y -q nvidia-container-toolkit",
            "sudo nvidia-ctk runtime configure --runtime=docker",
            "sudo systemctl restart docker",
        ]
    run_remote(handle, " && ".join(steps))


def pull(handle: str, image: str, region: str, gpu: bool = False) -> None:
    """Ensure the runtime, then ECR-login and docker-pull the image. This is the
    provisioning step -- its wall-clock belongs in `provision_s` (boot + runtime
    install + pull + stage), NOT `runtime_s`, so the timing split stays honest.
    Separated from run_container() precisely so the caller times it on the
    provision side.
    """
    ensure_runtime(handle, gpu)
    reg = _registry(image)
    login = (f"aws ecr get-login-password --region {shlex.quote(region)} "
             f"| sudo docker login --username AWS --password-stdin {shlex.quote(reg)}")
    run_remote(handle, f"{login} && sudo docker pull {shlex.quote(image)}")


def run_container(handle: str, image: str, env: dict[str, str], gpu: bool) -> str:
    """docker-run the benchmark image; the entrypoint is mdrun_wrapper.sh. This
    is the timed workload -- its wall-clock is `runtime_s`. The host work dir is
    bind-mounted so md*.log and the completion sentinel are visible to spawn's
    completion watcher and to fetch(). ns/day itself comes from GROMACS's own
    -resethway steady-state timers in the logs, not this wall-clock, so pull/boot
    overhead never contaminates the performance number.
    # GPU cells need the nvidia-container-toolkit, installed by ensure_runtime().
    """
    # WORK: container-side work dir. COMPLETION_FILE: point the wrapper's sentinel
    # at the bind-mounted dir so it appears on the host at COMPLETION_FILE (which
    # is what spawn --completion-file watches).
    ctr_env = {**env, "WORK": CTR_WORK,
               "COMPLETION_FILE": f"{CTR_WORK}/SPAWN_COMPLETE"}
    docker_env = " ".join(f"-e {k}={shlex.quote(v)}" for k, v in ctr_env.items())
    gpu_flag = "--gpus all " if gpu else ""
    # --privileged: MIG/MPS setup in the wrapper needs nvidia-smi -mig / control.
    priv = "--privileged " if gpu else ""
    cmd = (f"mkdir -p {HOST_WORK} && sudo docker run --rm {gpu_flag}{priv}"
           f"-v {HOST_WORK}:{CTR_WORK} {docker_env} {shlex.quote(image)} "
           f"bash /opt/bench/mdrun_wrapper.sh")
    return run_remote(handle, cmd)


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
    sudo: the container runs as root, so the bind-mounted logs are root-owned.
    # SPORE: unconfirmed until the Phase-1 live cell. If this proves flaky, the
    # robust alternative is to have mdrun_wrapper.sh push logs to S3 and fetch
    # from there (the instance already has awscli + the configured bucket).
    """
    remote = f"sudo tar czf - {remote_path} 2>/dev/null || true"
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
