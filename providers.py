"""Execution providers. Each returns (wait_seconds, runtime_seconds).

The split matters: wait and runtime are different arguments. Runtime is a
hardware claim. Wait is a scheduling-model claim, and it is where the on-prem
comparison actually lives -- so it is measured separately, reported as a
distribution, and always accompanied by a best case so the favorable framing is
given away up front.

Cloud has a queue too. Contended capacity -- spot pools, scarce GPU families,
quota ceilings -- means an instance request can sit unfulfilled exactly as a
batch job sits pending. So wait is decomposed the same way on both sides:

  acquire_s    time until the resource is GRANTED
                 cloud  = capacity wait (retry/backoff, InsufficientCapacity)
                 onprem = scheduler queue delay
  provision_s  time from grant to ready-to-compute
                 cloud  = boot + image pull + stage
                 onprem = prolog + node setup (usually ~0, folded into runtime)
  wait_s       = acquire_s + provision_s

Both sides are sampled as distributions, and both can fail outright:
capacity that never arrives is `infeasible:capacity` regardless of provider.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import statistics
import subprocess
import time
from dataclasses import dataclass, field

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


class CapacityUnavailable(RuntimeError):
    """Resource never granted within the allowed window. Same outcome class on
    either side of the fence: infeasible:capacity."""


@dataclass
class Timing:
    runtime_s: float = 0.0
    acquire_s: float = 0.0         # until granted (capacity wait / queue delay)
    capacity_seen_s: float = 0.0   # until capacity APPEARED (lagotto watch fire)
    acquire_method: str = ""       # lagotto | retry
    provision_s: float = 0.0       # grant -> ready to compute
    wait_s: float = 0.0            # acquire + provision
    wait_s_best: float = 0.0       # best case observed (benefit of the doubt)
    wait_s_p50: float = 0.0
    wait_s_p90: float = 0.0
    wait_samples: list[float] = field(default_factory=list)

    def total(self) -> None:
        self.wait_s = self.acquire_s + self.provision_s

    def summarize(self) -> None:
        self.total()
        if not self.wait_samples:
            return
        s = sorted(self.wait_samples)
        self.wait_s_best = s[0]
        self.wait_s_p50 = statistics.median(s)
        self.wait_s_p90 = s[min(len(s) - 1, int(0.9 * len(s)))]


def _sh(cmd: list[str]) -> str:
    if DRY_RUN:
        print(f"[dry-run] {' '.join(shlex.quote(c) for c in cmd)}")
        return ""
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


# --------------------------------------------------------------------------
# on-prem (Slurm)
# --------------------------------------------------------------------------
_JOBID = re.compile(r"(\d+)")


def onprem_submit(inst: dict, env: dict[str, str], workdir: str) -> str:
    """Submit the wrapper as a batch job. Returns job id."""
    exports = ",".join(f"{k}={v}" for k, v in env.items())
    cmd = [inst.get("submit", "sbatch"),
           "--parsable",
           f"--partition={inst.get('partition', 'gpu')}",
           f"--chdir={workdir}",
           f"--export=ALL,{exports}",
           "mdrun_wrapper.sh"]
    out = _sh(cmd)
    if DRY_RUN:
        return "dry-job"
    m = _JOBID.search(out)
    if not m:
        raise RuntimeError(f"could not parse job id from: {out!r}")
    return m.group(1)


def onprem_wait_and_run(inst: dict, env: dict[str, str], workdir: str,
                        poll_s: int = 15) -> Timing:
    """Submit, then measure queue wait and runtime SEPARATELY via sacct."""
    t = Timing()
    job = onprem_submit(inst, env, workdir)
    if DRY_RUN:
        return Timing(runtime_s=0.0, wait_s=0.0)

    while True:
        state = _sh(["sacct", "-j", job, "-n", "-P", "-o", "State"]).split("\n")[0].strip()
        if state.startswith(("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")):
            break
        time.sleep(poll_s)

    # Authoritative timestamps from the scheduler, not wall-clock guesses.
    fields = _sh(["sacct", "-j", job, "-n", "-P", "-X",
                  "-o", "Submit,Start,End,State"]).split("\n")[0].split("|")
    sub, start, end, state = fields[0], fields[1], fields[2], fields[3]
    if state.startswith("COMPLETED"):
        t.wait_s = _epoch(start) - _epoch(sub)
        t.runtime_s = _epoch(end) - _epoch(start)
    else:
        raise RuntimeError(f"job {job} ended {state}")
    t.wait_samples = [t.wait_s]
    t.summarize()
    return t


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))


def onprem_probe_queue(inst: dict, n: int | None = None) -> list[float]:
    """Submit n trivial jobs to the same partition to sample the wait
    distribution independently of the benchmark runs. Report best case AND
    p50/p90 -- giving away the favorable framing is the point."""
    n = n or int(inst.get("queue_samples", 0))
    samples: list[float] = []
    for _ in range(n):
        if DRY_RUN:
            print(f"[dry-run] probe submit to {inst.get('partition')}")
            continue
        job = _sh([inst.get("submit", "sbatch"), "--parsable",
                   f"--partition={inst.get('partition', 'gpu')}",
                   "--wrap=true"]).strip()
        while True:
            f = _sh(["sacct", "-j", job, "-n", "-P", "-X",
                     "-o", "Submit,Start,State"]).split("\n")[0].split("|")
            if f[2].startswith(("COMPLETED", "FAILED")):
                samples.append(_epoch(f[1]) - _epoch(f[0]))
                break
            time.sleep(10)
    return samples


# --------------------------------------------------------------------------
# local
# --------------------------------------------------------------------------
def local_run(env: dict[str, str], workdir: str) -> Timing:
    t0 = time.time()
    if DRY_RUN:
        print(f"[dry-run] local run in {workdir}")
        return Timing()
    subprocess.run(["bash", "mdrun_wrapper.sh"], check=True,
                   env={**os.environ, **env, "WORK": workdir})
    return Timing(runtime_s=time.time() - t0, wait_s=0.0)


# --------------------------------------------------------------------------
# cloud capacity: acquisition is a queue, so measure it like one
# --------------------------------------------------------------------------
def cloud_acquire(spawn_fn, max_wait_s: float, backoff_s: float = 20.0):
    """Retry a spawn until capacity is granted. Returns (handle, acquire_s).

    Raises CapacityUnavailable if the window expires -- which is the cloud-side
    equivalent of a job that never leaves the queue, and is reported with the
    same outcome class.
    """
    t0 = time.time()
    attempt = 0
    while True:
        try:
            handle = spawn_fn()
            return handle, time.time() - t0
        except Exception as e:
            msg = str(e)
            transient = any(k in msg for k in (
                "InsufficientInstanceCapacity", "InsufficientHostCapacity",
                "capacity-not-available", "MaxSpotInstanceCountExceeded",
                "Unsupported", "RequestLimitExceeded"))
            if not transient:
                raise
            if time.time() - t0 >= max_wait_s:
                raise CapacityUnavailable(
                    f"no capacity after {max_wait_s:.0f}s ({attempt} attempts): {msg[:120]}") from e
            attempt += 1
            if DRY_RUN:
                return "dry-handle", 0.0
            time.sleep(min(backoff_s * (2 ** min(attempt, 4)), 300))


def cloud_probe_capacity(instance_type: str, region: str, n: int,
                         max_wait_s: float, spawn_fn_factory) -> list[float]:
    """Sample the capacity-acquisition distribution for a pool, independently of
    the benchmark runs -- the cloud-side twin of onprem_probe_queue. Failed
    acquisitions are recorded as max_wait_s so the distribution is not silently
    biased toward the lucky draws."""
    samples: list[float] = []
    for _ in range(n):
        if DRY_RUN:
            print(f"[dry-run] capacity probe {instance_type} in {region}")
            continue
        try:
            handle, dt = cloud_acquire(spawn_fn_factory(), max_wait_s)
            samples.append(dt)
            try:
                _sh(["spawn", "down", handle])
            except Exception:
                pass
        except CapacityUnavailable:
            samples.append(max_wait_s)
    return samples


# --------------------------------------------------------------------------
# lagotto: watch for capacity instead of discovering it by failing
# --------------------------------------------------------------------------
# The retry loop below measures capacity wait by failing repeatedly: backoff
# granularity blurs the timestamp and every probe is a real launch attempt.
# lagotto watches the pool and fires when capacity appears, so acquire_s is
# directly observed and sampling a distribution costs nothing.
#
# Boundary that must be preserved: lagotto reports when capacity APPEARED, not
# when our request was GRANTED. Under contention these differ. Keep acquire_s as
# watch-start -> instance-running so it stays comparable to sacct Submit->Start,
# and record the fire time separately as capacity_seen_s.

def lagotto_acquire(instance_type: str, region: str, max_wait_s: float,
                    image: str, ttl_minutes: int, idle_minutes: int,
                    name: str) -> tuple[str, float, float]:
    """Watch a pool and launch when capacity appears.

    Returns (handle, acquire_s, capacity_seen_s).
    Raises CapacityUnavailable if the watch never fires inside the window.
    """
    t0 = time.time()
    # SPORE: confirm real lagotto syntax. Assumed: blocks until capacity, then
    # launches, emitting JSON with a handle and the moment capacity was seen.
    cmd = ["lagotto", "watch",
           "--type", instance_type,
           "--region", region,
           "--max-wait", f"{int(max_wait_s)}s",
           "--launch",
           "--image", image,
           "--ttl", f"{ttl_minutes}m",
           "--idle", f"{idle_minutes}m",
           "--name", name,
           "--json"]
    if DRY_RUN:
        print(f"[dry-run] {' '.join(shlex.quote(c) for c in cmd)}")
        return f"dry-{name}", 0.0, 0.0

    try:
        out = _sh(cmd)
    except subprocess.CalledProcessError as e:
        raise CapacityUnavailable(
            f"lagotto watch failed for {instance_type} in {region}: "
            f"{(e.stderr or '')[:160]}") from e

    d = json.loads(out)
    if not d.get("handle"):
        raise CapacityUnavailable(
            f"no capacity for {instance_type} in {region} within {max_wait_s:.0f}s")

    acquire_s = time.time() - t0
    # GAP: if lagotto does not emit the fire timestamp, capacity_seen_s collapses
    # to acquire_s and the appeared-vs-granted split is lost. See GAPS.md.
    seen = float(d.get("capacity_seen_s", d.get("waited_s", acquire_s)))
    return d["handle"], acquire_s, seen


def lagotto_probe(instance_type: str, region: str, n: int,
                  max_wait_s: float) -> list[float]:
    """Sample time-to-capacity by watching only -- no launches, so building a
    distribution is free. Non-firing watches record at the ceiling rather than
    being dropped, so the distribution is not biased toward lucky draws."""
    samples: list[float] = []
    for _ in range(n):
        cmd = ["lagotto", "watch", "--type", instance_type, "--region", region,
               "--max-wait", f"{int(max_wait_s)}s", "--json"]
        if DRY_RUN:
            print(f"[dry-run] {' '.join(shlex.quote(c) for c in cmd)}")
            continue
        t0 = time.time()
        try:
            d = json.loads(_sh(cmd))
            samples.append(float(d.get("capacity_seen_s", time.time() - t0)))
        except Exception:
            samples.append(max_wait_s)
    return samples
