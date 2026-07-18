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
from datetime import UTC, datetime

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


def _epoch_utc(ts: str) -> float:
    """Parse a lagotto ISO-8601 UTC timestamp (`...Z` or `+00:00`) to an epoch.
    Distinct from _epoch(): sacct emits naive local-time stamps, lagotto emits
    zoned UTC. Feeding one to the other's parser raises or is silently wrong."""
    return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone(UTC).timestamp())


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
# The retry loop above measures capacity wait by failing repeatedly: backoff
# granularity blurs the timestamp and every probe is a real launch attempt.
# lagotto watches the pool and records when capacity APPEARED, so acquire_s is
# directly observed and sampling a distribution costs nothing.
#
# Command syntax verified 2026-07-18 via `lagotto {watch,poll,status,history}
# --help`. The real tool is ASYNC and DynamoDB-backed, NOT the blocking
# `watch --launch` the first draft assumed:
#   * `lagotto watch <pattern> --action {notify,spawn,hold} [--spot] --ttl ...`
#     registers a watch and returns its id. --action spawn needs a spawn
#     LaunchConfig YAML (--spawn-config); there is no --launch/--image/--max-wait.
#   * A poller acts on watches: `lagotto poll --daemon --interval` locally, or a
#     Lambda schedule in production. `lagotto watch` auto-creates the tables.
#   * `lagotto history --watch-id <id> -o json` records matches with timestamps;
#     `lagotto status <id>` shows current state.
#
# Boundary that must be preserved (this is exactly GAP G1): lagotto reports when
# capacity APPEARED, not when our request was GRANTED. Keep acquire_s as
# watch-start -> instance-running (comparable to sacct Submit->Start) and record
# the fire time separately as capacity_seen_s from the history record.
#
# Command lines AND the `history` record schema are verified against live output
# (2026-07-18): matches carry `watch_id`, `matched_at` (ISO-8601 UTC), and
# `availability_zone`. What remains unverified until a live capacity event is the
# END-TO-END flow: that `--action hold` + our own `poll --mine --watch` actually
# produces a match record we can then launch against. Watch id is parsed from
# `watch` JSON, whose shape was not directly observed (empty account).
# SPORE: end-to-end flow unconfirmed. `watch` id field (`watch_id`/`id`) assumed;
# lookups are defensive so a shape mismatch degrades rather than crashes. Until
# Phase 1 exercises this, `use_lagotto: false` uses the fully verified retry path.
# Schema-drift risk tracked as G10 (#39).

_LAGOTTO_POLL_S = 15


def _lagotto_watch_id(instance_type: str, region: str, spot: bool,
                      ttl_minutes: int, extra: list[str]) -> str:
    cmd = ["lagotto", "watch", instance_type, "--regions", region,
           "--ttl", f"{ttl_minutes}m", "-o", "json", *extra]
    if spot:
        cmd.append("--spot")
    out = _sh(cmd)
    if DRY_RUN:
        return "dry-watch"
    d = json.loads(out)
    wid = d.get("watch_id") or d.get("id")
    if not wid:
        raise CapacityUnavailable(f"lagotto watch did not return an id: {out[:160]}")
    return wid


def _lagotto_fired(watch_id: str) -> dict | None:
    """Return the earliest match record for a watch, or None if it hasn't fired.

    Verified against live `lagotto history -o json` (2026-07-18): each record has
    `watch_id`, `matched_at` (ISO-8601 UTC, e.g. 2026-06-28T10:19:32.869772Z),
    `availability_zone` (answers GAP G4), `instance_type`, `price`, `is_spot`,
    and `action_taken` in {spawned, spawn_failed}. A record means capacity
    APPEARED regardless of action_taken -- which is what we want, since with
    --action hold we do the launch ourselves. Sort by matched_at so we take the
    first appearance, not whatever order the API returns.
    """
    out = _sh(["lagotto", "history", "--watch-id", watch_id, "-o", "json"])
    rows = json.loads(out) if out else []
    if not rows:
        return None
    return min(rows, key=lambda r: r.get("matched_at", ""))


def lagotto_acquire(instance_type: str, region: str, max_wait_s: float,
                    image: str, ttl_minutes: int, idle_minutes: int,
                    name: str) -> tuple[str, float, float]:
    """Register a watch, drive the poller, and launch when capacity appears.

    Returns (handle, acquire_s, capacity_seen_s).
    Raises CapacityUnavailable if the watch never fires inside the window.

    SPORE: end-to-end unverified (needs the DynamoDB backend + a real capacity
    event). Uses --action hold so we control the launch (via spore.spawn) and
    keep acquire_s = watch-start -> instance-running.
    """
    t0 = time.time()
    if DRY_RUN:
        print(f"[dry-run] lagotto watch {instance_type} --regions {region} "
              f"--action hold --ttl {ttl_minutes}m -o json  (+ poll/history)")
        return f"dry-{name}", 0.0, 0.0

    watch_id = _lagotto_watch_id(instance_type, region, spot=False,
                                 ttl_minutes=ttl_minutes, extra=["--action", "hold"])
    match = None
    while match is None:
        if time.time() - t0 >= max_wait_s:
            _sh(["lagotto", "cancel", watch_id])
            raise CapacityUnavailable(
                f"no capacity for {instance_type} in {region} within {max_wait_s:.0f}s")
        _sh(["lagotto", "poll", "--mine", "--watch", watch_id])
        match = _lagotto_fired(watch_id)
        if match is None:
            time.sleep(_LAGOTTO_POLL_S)

    # Capacity appeared. Record the fire moment (G1), then launch ourselves.
    seen_at = match["matched_at"]  # verified field (lagotto history)
    capacity_seen_s = (_epoch_utc(seen_at) - t0 if seen_at else time.time() - t0)
    from spore import spawn as _spawn
    handle = _spawn(instance_type, image, ttl_minutes, idle_minutes, region, name)
    acquire_s = time.time() - t0
    return handle, acquire_s, capacity_seen_s


def lagotto_probe(instance_type: str, region: str, n: int,
                  max_wait_s: float) -> list[float]:
    """Sample time-to-capacity by watching only (--action notify, no launch), so
    building a distribution is free. Non-firing watches record at the ceiling
    rather than being dropped, so the distribution is not biased toward lucky
    draws.

    SPORE: end-to-end unverified; same backend dependency as lagotto_acquire.
    """
    samples: list[float] = []
    for _ in range(n):
        if DRY_RUN:
            print(f"[dry-run] lagotto watch {instance_type} --regions {region} "
                  f"--action notify -o json  (+ poll/history)")
            continue
        t0 = time.time()
        try:
            wid = _lagotto_watch_id(instance_type, region, spot=True,
                                    ttl_minutes=max(1, int(max_wait_s // 60)),
                                    extra=["--action", "notify"])
            match = None
            while match is None and time.time() - t0 < max_wait_s:
                _sh(["lagotto", "poll", "--mine", "--watch", wid])
                match = _lagotto_fired(wid)
                if match is None:
                    time.sleep(_LAGOTTO_POLL_S)
            if match is None:
                _sh(["lagotto", "cancel", wid])
                samples.append(max_wait_s)
            else:
                seen_at = match["matched_at"]  # verified field (lagotto history)
                samples.append(_epoch_utc(seen_at) - t0 if seen_at else time.time() - t0)
        except Exception:
            samples.append(max_wait_s)
    return samples
