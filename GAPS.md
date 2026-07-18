# Capability gaps — file as issues when this moves to Claude Code

Measurements the harness wants but may not be exposed. Each is written as the
issue text, with why the comparison degrades without it.

## lagotto

**G1. Emit the capacity-fire timestamp distinctly from launch completion.**
`acquire_s` (watch-start → instance running) and `capacity_seen_s` (watch-start →
capacity appeared) are different quantities under contention. Without the split,
`capacity_seen_s` collapses to `acquire_s` and we lose the appeared-vs-granted
distinction — the thing that makes cloud `acquire_s` legitimately comparable to
`sacct` Submit→Start.
*Wanted:* JSON field for the fire moment, and for the interval between fire and
running.

**G2. Watch-only mode that never launches.**
Sampling a time-to-capacity distribution should cost nothing. If `--json`
without `--launch` isn't supported, every distribution sample becomes a real
launch and `wait_samples: 10` on g7e gets expensive.

**G3. Report whether the watch expired vs. errored.**
Currently any nonzero exit is treated as `infeasible:capacity`. A malformed
request and a genuinely exhausted pool are different findings and shouldn't
share an outcome class.

**G4. Per-AZ / per-pool granularity in the watch result.**
Which AZ the capacity appeared in is the input to heterogeneous placement (D11).
Aggregated region-level results can't drive that.

**G5. Historical time-to-capacity, not just live watching.**
Building a distribution live takes as long as the distribution is wide. If
lagotto retains observations, `wait_samples` could be answered from history.

## truffle

**G6. Spot interruption hazard per pool, not just price.**
D9 (shard length vs spot economics) needs an interruption rate to compute Daly's
optimal checkpoint interval. Price alone is insufficient. Spot placement score
would be a usable proxy if exposed.

**G7. Quota headroom, not just quota limit.**
Whether a launch will be quota-blocked is knowable before attempting. That's an
`infeasible:capacity` we could predict rather than discover.

## spawn

**G8. Separate boot-complete from image-pull-complete.**
`provision_s` currently lumps boot + pull + stage. For D10 (worker-pull vs
instance-per-shard) we need to attribute the amortizable portion specifically.

**G9. Structured spot-interruption event with timestamp.**
Needed to reconcile incurred cost against wasted work in `$/result`.

## Harness-side, not a tool gap

- `providers._epoch()` assumes `sacct` emits `%Y-%m-%dT%H:%M:%S` in local time.
  Verify per site before any wait number is trusted.
- All `# SPORE:` marked CLI invocations are assumed syntax and need confirming
  against the installed tools.
