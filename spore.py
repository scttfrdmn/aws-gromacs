"""Thin wrappers around spore.host tools (truffle / spawn).

The exact CLI flags for truffle/spawn are marked `# SPORE:` — confirm them
against your installed spore.host CLI (or swap these bodies for MCP calls).
Every function honors DRY_RUN, which just prints the command it would run.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def _run(cmd: list[str]) -> str:
    printable = " ".join(shlex.quote(c) for c in cmd)
    if DRY_RUN:
        print(f"[dry-run] {printable}")
        return "{}"
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


@dataclass
class Prices:
    on_demand_hr: float
    spot_hr: float


def truffle_price(instance_type: str, region: str) -> Prices:
    """Return on-demand and current spot $/hr for an instance type."""
    # SPORE: adjust to real truffle syntax; assumed to emit JSON.
    out = _run(["truffle", "price", "--type", instance_type,
                "--region", region, "--json"])
    if DRY_RUN:
        return Prices(on_demand_hr=1.0, spot_hr=0.4)
    d = json.loads(out)
    return Prices(on_demand_hr=float(d["on_demand"]), spot_hr=float(d["spot"]))


def spawn(instance_type: str, image: str, ttl_minutes: int, idle_minutes: int,
          region: str, name: str) -> str:
    """Launch an on-demand instance with auto-termination. Returns a handle."""
    # SPORE: adjust flags. Timing runs are on-demand (no --spot) on purpose.
    out = _run(["spawn", "up",
                "--type", instance_type,
                "--image", image,
                "--ttl", f"{ttl_minutes}m",
                "--idle", f"{idle_minutes}m",
                "--region", region,
                "--name", name,
                "--json"])
    if DRY_RUN:
        return f"dry-{name}"
    return json.loads(out)["handle"]


def run_remote(handle: str, command: str, env: dict[str, str] | None = None) -> str:
    """Execute a shell command on the spawned instance, return its stdout."""
    env = env or {}
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    full = f"{env_prefix} {command}".strip()
    # SPORE: adjust to real remote-exec syntax.
    return _run(["spawn", "exec", handle, "--", "bash", "-lc", full])


def fetch(handle: str, remote_path: str, local_path: str) -> None:
    """Copy a file off the instance."""
    # SPORE: adjust to real copy syntax.
    _run(["spawn", "cp", f"{handle}:{remote_path}", local_path])


def terminate(handle: str) -> None:
    # SPORE: TTL will also reap it, but tear down explicitly on success/failure.
    _run(["spawn", "down", handle])
