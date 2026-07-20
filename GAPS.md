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
*Verified 2026-07-18:* `lagotto history` provides `matched_at` (capacity
appeared) but **no** running/granted timestamp, so under `--action spawn` the
split is lost exactly as feared. The harness works around it with `--action
hold` + its own launch wall-clock. *Wanted:* a `running_at` / fire→running
interval in the `--action spawn` record.

**G2. Watch-only mode that never launches.** — *RESOLVED (verified 2026-07-18).*
`lagotto watch <type> --action notify` records availability without launching,
and `--action hold` records without acting at all, so sampling a time-to-capacity
distribution costs nothing. The original premise (a blocking `watch --launch`)
was wrong; the tool is async (register a watch, a poller acts on it).

**G3. Report whether the watch expired vs. errored.**
Currently any nonzero exit is treated as `infeasible:capacity`. A malformed
request and a genuinely exhausted pool are different findings and shouldn't
share an outcome class.

**G4. Per-AZ / per-pool granularity in the watch result.** — *PARTLY RESOLVED.*
The `lagotto history` record carries `availability_zone` and `candidate_azs`
(verified 2026-07-18), which drives heterogeneous placement (D11). Still open:
per-pool depth, not just which AZ won a single match.

**G5. Historical time-to-capacity, not just live watching.** — *RESOLVED.*
`lagotto history [--watch-id <id>] -o json` returns retained match records with
`matched_at`, so `wait_samples` can be answered from history rather than only by
watching live. (Schema is undocumented — see G10.)

**G10. Document the history/status JSON schema.**
`lagotto history`/`status -o json` are the harness's only programmatic view of
whether/when a watch fired, but the record schema is documented nowhere. Field
names (`matched_at`, `availability_zone`, `watch_id`, `action_taken`) were
reverse-engineered from live output on 2026-07-18. If the schema drifts, parsing
breaks silently or a sample is dropped to the `max_wait_s` ceiling — biasing the
wait distribution with no error. *Wanted:* a documented, versioned schema.
Filed upstream-candidate; tracked as issue #39.

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

**G10. `--completion-file` + `--on-complete terminate` did not tear down (LOAD-BEARING).**
In the 2026-07-20 three-vendor run, **17 autonomous cells wrote the host
completion sentinel** (`/tmp/bench/SPAWN_COMPLETE`, the exact `--completion-file`
path) **yet none self-terminated** — and the 45-minute `--idle-timeout` did not
fire either; the instances ran for hours until manually terminated (only `--ttl`
would eventually have reaped them). spawn 0.83.1. This is the autonomous-cell
model's core safety mechanism, so it is load-bearing. Repro needs isolation:
(a) does spawn detect a `touch`-created (root-owned, via docker bind mount) file,
or does it need the file created after launch by the spored user? (b) does a
lingering `tee` process-substitution child (cell_runner.sh) keep the workload
looking non-idle? (c) is completion detection even armed when `--command` is a
long-running foreground process? Until resolved, the coordinator terminates every
cell explicitly after collecting results (run_matrix.py `finally: spore.terminate`)
— do NOT rely on autonomous teardown. Also: `spawn terminate <name>` (without
`--yes`) silently no-ops; `spore.terminate` uses `--yes`.

## Harness-side, not a tool gap

- `providers._epoch()` assumes `sacct` emits `%Y-%m-%dT%H:%M:%S` in local time.
  Verify per site before any wait number is trusted.
- All `# SPORE:` marked CLI invocations are assumed syntax and need confirming
  against the installed tools.
