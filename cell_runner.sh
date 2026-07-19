#!/usr/bin/env bash
# Runs ON a spawned AL2023 benchmark instance, passed via `spawn launch
# --command`. Fully autonomous: installs the container runtime, pulls the arch
# image, runs the GROMACS wrapper, records timing + logs to S3, and signals
# completion so spawn self-terminates the box. The coordinator never holds an
# SSH session -- it launches this and polls S3. (Mirrors build/build_and_push.sh,
# the pattern that ran 3-way parallel without the SSH contention that deadlocked
# the connect-driven approach.)
#
# Env in (all via --command):
#   IMAGE          ECR image URI to run
#   AWS_REGION     region (ECR login)
#   GPU            1 for GPU cells (install nvidia-container-toolkit, --gpus all)
#   RESULTS_S3     s3://.../results/<cell>/logs  (logs + timing.json land here)
#   WRAPPER_URL    raw URL of mdrun_wrapper.sh (mounted at runtime, not baked)
#   TPR_SRC NSTEPS MDRUN_FLAGS MIG_SLICES MIG_PROFILE MPS_PROCS REPLICATES
#                  -> passed through to the wrapper inside the container
#   COMPLETION_FILE  host sentinel for spawn --on-complete (touched last, on success)
set -euo pipefail

: "${IMAGE:?}" "${AWS_REGION:?}" "${RESULTS_S3:?}" "${WRAPPER_URL:?}"
GPU="${GPU:-0}"
COMPLETION_FILE="${COMPLETION_FILE:-/tmp/bench/SPAWN_COMPLETE}"
HOST_WORK=/tmp/bench
CTR_WORK=/work
CTR_WRAPPER=/opt/bench/mdrun_wrapper.sh
REGISTRY="${IMAGE%%/*}"
mkdir -p "$HOST_WORK"
TIMING="$HOST_WORK/timing.json"

# Epoch helpers so the coordinator can reconstruct the wait/runtime split from
# the instance itself (more accurate than coordinator wall-clock, and needs no
# SSH). boot = when this runner started (post spored/SSH-ready).
now() { date +%s; }
T_BOOT=$(now)

echo "== install docker =="
sudo dnf install -y -q docker
sudo systemctl enable --now docker
if [ "$GPU" = "1" ]; then
  echo "== install nvidia-container-toolkit =="
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
    | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
  sudo dnf install -y -q nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi

echo "== ecr login + pull =="
aws ecr get-login-password --region "$AWS_REGION" \
  | sudo docker login --username AWS --password-stdin "$REGISTRY"
sudo docker pull "$IMAGE"

echo "== stage current wrapper =="
curl -fsSL "$WRAPPER_URL" -o "$HOST_WORK/mdrun_wrapper.sh"
chmod +x "$HOST_WORK/mdrun_wrapper.sh"
T_READY=$(now)          # docker + image + wrapper ready == end of provisioning

# GPU cells: sample REAL device utilization during the timed run via DCGM (on
# the DLAMI already). NOT nvidia-smi's "utilization.gpu", which is just percent-
# of-time-a-kernel-ran and reports ~100% even when a kernel touches a few SMs --
# useless for the "is the card actually filled?" question that drives D3 and the
# thesis's "15% utilization" pathology. DCGM profiling fields measure the chip:
#   1002 = PROF_SM_ACTIVE   (fraction of SMs active -- true occupancy)
#   1005 = PROF_DRAM_ACTIVE (memory-bandwidth utilization -- the bandwidth story)
#   155  = power draw (W),  252 = memory used (MiB)
# Sampled 1/s to a CSV for the duration of the run, then summarized to gpuutil.json.
GPU_SAMPLE=""
if [ "$GPU" = "1" ] && command -v dcgmi >/dev/null; then
  GPU_SAMPLE="$HOST_WORK/gpu_samples.csv"
  ( sudo dcgmi dmon -e 1002,1005,155,252 -d 1000 2>/dev/null | tee "$GPU_SAMPLE" >/dev/null ) &
  DCGM_PID=$!
fi

echo "== run wrapper (timed workload) =="
gpu_args=()
[ "$GPU" = "1" ] && gpu_args=(--gpus all --privileged)
# Pass the benchmark env through to the wrapper; WORK/COMPLETION_FILE point at
# the bind-mounted dir so md*.log + the sentinel land on the host.
sudo docker run --rm "${gpu_args[@]}" \
  -v "$HOST_WORK:$CTR_WORK" \
  -v "$HOST_WORK/mdrun_wrapper.sh:$CTR_WRAPPER:ro" \
  -e "TPR_SRC=${TPR_SRC:?}" -e "NSTEPS=${NSTEPS:?}" \
  -e "MDRUN_FLAGS=${MDRUN_FLAGS:-}" \
  -e "MIG_SLICES=${MIG_SLICES:-0}" -e "MIG_PROFILE=${MIG_PROFILE:-}" \
  -e "MPS_PROCS=${MPS_PROCS:-0}" -e "REPLICATES=${REPLICATES:-3}" \
  -e "WORK=$CTR_WORK" -e "COMPLETION_FILE=$CTR_WORK/SPAWN_COMPLETE" \
  -e "RESULTS_S3=$RESULTS_S3" \
  "$IMAGE" bash "$CTR_WRAPPER"
T_DONE=$(now)

# Stop the sampler and summarize (mean/max SM-active, DRAM-active, power, mem).
if [ -n "$GPU_SAMPLE" ]; then
  sudo kill "$DCGM_PID" 2>/dev/null || true
  python3 - "$GPU_SAMPLE" "$HOST_WORK/gpuutil.json" <<'PY' || true
import sys, json, statistics
rows = []
with open(sys.argv[1]) as fh:
    for line in fh:
        p = line.split()
        # dcgmi dmon rows: "GPU <id> <smact> <drama> <power> <memused>"; skip headers
        if len(p) >= 6 and p[0] == "GPU":
            try:
                rows.append([float(p[2]), float(p[3]), float(p[4]), float(p[5])])
            except ValueError:
                pass
def col(i):
    vals = [r[i] for r in rows]
    return vals or [0.0]
out = {
    "samples": len(rows),
    "sm_active_mean": round(statistics.mean(col(0)), 4),
    "sm_active_max": round(max(col(0)), 4),
    "dram_active_mean": round(statistics.mean(col(1)), 4),
    "dram_active_max": round(max(col(1)), 4),
    "power_w_mean": round(statistics.mean(col(2)), 1),
    "mem_used_mib_max": round(max(col(3)), 0),
}
json.dump(out, open(sys.argv[2], "w"))
PY
  aws s3 cp "$HOST_WORK/gpuutil.json" "$RESULTS_S3/gpuutil.json" --only-show-errors || true
fi

echo "== write timing.json + push to S3 =="
# provision_s = boot->ready (install+pull+stage); runtime_s = the docker run.
# acquire_s (capacity wait) is measured coordinator-side from spawn timestamps.
cat > "$TIMING" <<EOF
{"boot_epoch": $T_BOOT, "ready_epoch": $T_READY, "done_epoch": $T_DONE,
 "provision_s": $((T_READY - T_BOOT)), "runtime_s": $((T_DONE - T_READY))}
EOF
aws s3 cp "$TIMING" "$RESULTS_S3/timing.json" --only-show-errors

# The wrapper already pushed md*.log to $RESULTS_S3 before its own sentinel; but
# it wrote the sentinel INSIDE the container. Signal completion on the HOST now,
# only on this success path, so spawn --on-complete terminates the box.
touch "$COMPLETION_FILE"
echo "== cell complete =="
