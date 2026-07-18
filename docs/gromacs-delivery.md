# GROMACS delivery decision (Phase 0, issue #2)

`spawn` boots an EC2 instance from an **AMI**, not a container image — but
`matrix.yaml` specifies five GROMACS *container* images. This document resolves
how GROMACS actually lands on a spawned instance. It is the upstream decision for
issues #1 (placeholders) and #2 (build images), and it determines the Phase-1
cell.

## What spawn actually offers (verified 2026-07-18 via `--help` + live AWS)

- **Auto-detected AMIs.** `spawn launch` auto-detects Amazon Linux 2023,
  *including GPU variants* — so NVIDIA drivers are present on GPU instances
  without us baking them. This is the single biggest simplifier.
- **`--command "<sh>"`** runs after `spored` setup, on every instance. This is
  our bootstrap hook.
- **`--attach-volume snap-xxx:/mount[:ro]`** mounts a pre-baked EBS volume
  (repeatable; read-only is the common case for shared reference data).
- **`spawn ami create <name>`** snapshots a running instance into a reusable
  spawn-managed AMI.
- **ECR is available** in this account (`aws ecr` works, valid auth token), so a
  private container registry the instances can pull from already exists.
- **`spawn app`** is for NICE DCV *streamed interactive* apps — not our headless
  batch case. Not used.

## Why we keep the containers (not a generic GROMACS AMI)

The five Dockerfiles are not incidental packaging — they encode the experiment:

| image | SIMD build | why it exists |
|-------|-----------|---------------|
| intel | `AVX_512` | Intel baseline |
| amd | `AVX_512`, `-march=znver4` | AMD full-width |
| amd-avx2 | `AVX2_256` | **the "software beats hardware" arm** on identical AMD silicon |
| arm | `ARM_SVE`, `-mcpu=neoverse-v2` | Graviton4 |
| cuda | `GMX_GPU=CUDA` | all GPU cells (whole-card + MIG) |

The `amd` vs `amd-avx2` pair is a load-bearing thesis comparison (D2-adjacent:
the SIMD build choice may beat the instance choice). A single generic build would
delete it. So the per-arch build must survive whatever delivery mechanism we pick.

## Options considered

### A. Container-on-boot via `--command` (RECOMMENDED)
Push the five images to ECR (Phase 0). At launch, `--command` logs into ECR,
`docker run`s the arch-appropriate image, mounting the work dir; the container's
`/opt/bench/mdrun_wrapper.sh` runs the replicates and writes the completion
sentinel to the host.

- **+** Reuses the existing Dockerfiles verbatim — SIMD arms preserved.
- **+** No AMI baking; one registry, five tags; rebuild = `docker push`.
- **+** GPU AMI already has drivers; `--gpus all` + nvidia container toolkit.
- **-** Per-run image pull latency (mitigated: pull counts as `provision_s`,
  which we already measure; and D10 explicitly studies boot/pull amortization).
- **-** `--command` must map `matrix.yaml` env (TPR_SRC, NSTEPS, …) into
  `docker run -e`. Mechanical.

### B. Pre-baked AMI per arch (`spawn ami create`)
Launch once per arch, install GROMACS, snapshot to an AMI; `matrix.yaml`
`images:` become AMI ids.

- **+** No per-run pull; fastest `provision_s`.
- **-** Five AMIs to build/maintain/rebuild on every GROMACS or flag change —
  slower iteration than `docker push`.
- **-** GPU vs CPU AMIs diverge; more moving parts.
- Reasonable later optimization if pull latency dominates, but heavier now.

### C. Attached EBS snapshot of `/opt/gromacs`
Build once, snapshot the install volume, `--attach-volume ...:ro`.

- **-** Binaries built on one instance type may assume a CPU baseline; the whole
  point is arch-specific SIMD, so a shared volume fights the experiment. Rejected.

## Decision

**Option A (container-on-boot).** It preserves the SIMD experiment, reuses the
Dockerfiles, keeps iteration fast, and its one downside (pull latency) is already
inside a quantity the harness measures and a demo (D10) explicitly studies.

## Concrete Phase-0 changes this implies

1. **ECR repo + five tags.** e.g. `${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/gromacs-bench:{intel-avx512,amd-avx512,amd-avx2,arm-sve,cuda}` (account/region come from the env so nothing account-specific is committed). These are already the `images:` URIs in `matrix.yaml` (resolves #1's image placeholders).
2. **`spore.spawn` gains a `--command`** that: ECR-login, `docker run` the passed image with the cell's env and (GPU cells) `--gpus all`, bind-mounting a host work dir so logs + `/tmp/SPAWN_COMPLETE` land where spawn and `fetch()` expect them.
3. **Region.** ECR/`truffle`/`spawn` should agree. `matrix.yaml` says `us-east-1`; the shell default is `us-west-2`. Pick one for the run (recommend `us-east-1` to match the matrix) and set it explicitly.
4. **`s3_bucket`** for `.tpr` staging (#3) and results sync — still required.
5. GPU container runtime: confirm the AL2023 GPU AMI has the nvidia container toolkit, or add its install to `--command` (one-liner) — verify in the Phase-1 cell.

## What stays unverified until the paid Phase-1 cell
- That `docker run --gpus all` works on the auto-detected GPU AMI out of the box.
- Exact `--command` env-passing and that the container's sentinel write is
  visible to the host spawn agent.
These are the `# SPORE:` end-to-end items; Phase 1 (`small` on `c8g`, then one
GPU cell) exercises them before the sweep.
