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
