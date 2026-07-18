#!/usr/bin/env bash
# Runs on the target machine (spawned instance or local box). Stages a .tpr,
# runs GROMACS per config, leaves md*.log under $WORK. parse_log.py reads them.
#
# Env in:
#   TPR_SRC      s3://bucket/.../benchMEM-hmr.tpr  OR  /local/path/benchMEM.tpr
#   NSTEPS       timed run length (int)
#   MDRUN_FLAGS  verbatim flags from the config
#   MIG_SLICES   0 | 2 | 4          (one sim per slice, ns/day summed)
#   MIG_PROFILE  2g.48gb | 1g.24gb  (when MIG_SLICES>0)
#   MPS_PROCS    0 | N              (N concurrent sims sharing one GPU via MPS)
#   REPLICATES   N timed runs (default 3). ns/day is noisy; one run is not a
#                measurement. parse_log.py turns the replicates into mean +/- CI.
#   GMX          gmx binary (default: gmx)
#
# Every log is named md_rep<r>_*.log so parse_log.py can separate the
# across-replicate distribution from the within-replicate slice sum.
set -euo pipefail

GMX="${GMX:-gmx}"
WORK="${WORK:-/tmp/bench}"
MIG_SLICES="${MIG_SLICES:-0}"
MPS_PROCS="${MPS_PROCS:-0}"
REPLICATES="${REPLICATES:-3}"
mkdir -p "$WORK"; cd "$WORK"

case "$TPR_SRC" in
  s3://*) aws s3 cp "$TPR_SRC" ./run.tpr ;;
  *)      cp "$TPR_SRC" ./run.tpr ;;
esac

# -resethway: reset timers past load-balancing warmup, steady state only.
COMMON=(-s run.tpr -nsteps "$NSTEPS" -resethway -noconfout)
read -r -a FLAGS <<< "${MDRUN_FLAGS:-}"

# $1 = replicate index. One whole-device run; log tagged so parse_log.py can
# treat replicates as a distribution.
run_single() {
  local rep="$1"
  "$GMX" mdrun "${COMMON[@]}" "${FLAGS[@]}" -deffnm "md_rep${rep}"
}

# Launch N concurrent sims for one replicate, each in rep<r>/slice_<i>/,
# optionally pinned to a device. ns/day is summed across slices per replicate.
run_parallel() {
  local rep="$1"; local n="$2"; shift 2
  local devices=("$@")          # empty => all share the default device (MPS)
  local pids=() rc=0
  for ((i=0; i<n; i++)); do
    mkdir -p "rep${rep}/slice_$i"
    ( cd "rep${rep}/slice_$i"
      if [ "${#devices[@]}" -gt 0 ]; then
        export CUDA_VISIBLE_DEVICES="${devices[$i]}"
      fi
      "$GMX" mdrun -s ../../run.tpr -nsteps "$NSTEPS" -resethway -noconfout \
        "${FLAGS[@]}" -deffnm "md_rep${rep}_$i"
    ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  return $rc
}

setup_mig() {
  sudo nvidia-smi -mig 1 >/dev/null
  sudo nvidia-smi mig -dci >/dev/null 2>&1 || true
  sudo nvidia-smi mig -dgi >/dev/null 2>&1 || true
  local p; p=$(printf "%s," $(yes "$MIG_PROFILE" | head -n "$MIG_SLICES")); p=${p%,}
  sudo nvidia-smi mig -cgi "$p" -C >/dev/null
  mapfile -t UUIDS < <(nvidia-smi -L | grep -oE 'MIG-[0-9a-f-]+')
  [ "${#UUIDS[@]}" -eq "$MIG_SLICES" ] || {
    echo "expected $MIG_SLICES MIG devices, found ${#UUIDS[@]}" >&2; exit 1; }
}

# MIG/MPS device setup is done once; only the timed mdrun repeats per replicate.
if [ "$MIG_SLICES" -gt 0 ]; then
  setup_mig
  for ((r=0; r<REPLICATES; r++)); do run_parallel "$r" "$MIG_SLICES" "${UUIDS[@]}"; done
elif [ "$MPS_PROCS" -gt 0 ]; then
  nvidia-cuda-mps-control -d || true
  for ((r=0; r<REPLICATES; r++)); do run_parallel "$r" "$MPS_PROCS"; done
  echo quit | nvidia-cuda-mps-control || true
else
  for ((r=0; r<REPLICATES; r++)); do run_single "$r"; done
fi

# Collect logs to a flat location for retrieval.
mkdir -p "$WORK/logs"
find "$WORK" -name 'md*.log' -not -path "$WORK/logs/*" -exec cp {} "$WORK/logs/" \;
