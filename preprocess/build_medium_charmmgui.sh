#!/usr/bin/env bash
# Turn a CHARMM-GUI Membrane Builder GROMACS export into the `medium` benchmark
# tprs (base 2 fs + HMR 4 fs). Runs ON a spawned instance in the CUDA gmx image.
#
# Why a separate script from build_medium.sh: a CHARMM-GUI system ships its OWN
# topology (topol.top + toppar/ CHARMM36m) and its OWN equilibration .mdp chain.
# We do NOT re-run pdb2gmx or supply an AMBER FF -- we use CHARMM-GUI's topology
# as-is (that's the whole point of letting the standard tool build the membrane),
# run its equilibration, then grompp two production tprs from the equilibrated
# system. A real, correctly-built atomistic membrane-protein system.
#
# Env in:
#   CGUI_S3       s3://.../charmm-gui-medium.tgz   (the CHARMM-GUI GROMACS export)
#   TPR_S3        s3://.../gromacs-bench/tpr       (uploads channel-medium{,-hmr}.tpr)
#   SYS           output basename (default channel-medium)
#   MDRUN_NB      gpu | cpu (default cpu; equilibration only)
#   COMPLETION_FILE  sentinel for spawn --on-complete (touched on success only)
set -euo pipefail

CGUI_S3="${CGUI_S3:?set CGUI_S3}"
TPR_S3="${TPR_S3:?set TPR_S3}"
SYS="${SYS:-channel-medium}"
MDRUN_NB="${MDRUN_NB:-cpu}"
COMPLETION_FILE="${COMPLETION_FILE:-/tmp/SPAWN_COMPLETE}"
GMX="${GMX:-gmx}"
WORK="${WORK:-/tmp/build}"
mkdir -p "$WORK"; cd "$WORK"

# Build log -> S3 on exit (success or failure), same debuggability as the cells.
LOG="$WORK/build_medium_cgui.log"
exec > >(tee -a "$LOG") 2>&1
trap 'aws s3 cp "$LOG" "$TPR_S3/build_medium_cgui.log" --only-show-errors 2>/dev/null || true' EXIT

echo "== fetch + unpack CHARMM-GUI archive =="
aws s3 cp "$CGUI_S3" cgui.tgz --only-show-errors
tar xzf cgui.tgz
# CHARMM-GUI lays out gromacs/ under charmm-gui-<id>/; find it robustly.
GRO_DIR=$(dirname "$(find . -name step5_input.gro -path '*gromacs*' | head -1)")
[ -n "$GRO_DIR" ] && [ -d "$GRO_DIR" ] || { echo "no gromacs/ dir with step5_input.gro"; exit 1; }
cd "$GRO_DIR"
echo "== using CHARMM-GUI gromacs dir: $GRO_DIR =="
ls

# CHARMM-GUI ships a numbered equilibration chain (step6.0_minimization,
# step6.1..6.6_equilibration) then step7_production. Run its own chain verbatim
# -- it is tuned for the membrane (graduated position restraints). This is
# equilibration; MDRUN_NB just sets build speed, not the benchmark.
run_mdp() {  # $1 = mdp basename (no ext), $2 = input gro, $3 = optional -t cpt
  local mdp="$1" cin="$2" cpt="${3:-}"
  local ndx=(); [ -f index.ndx ] && ndx=(-n index.ndx)
  # -t only if the checkpoint actually exists: steep minimization (step6.0)
  # writes no .cpt, so the first equilibration step must grompp without -t.
  local tflag=(); [ -n "$cpt" ] && [ -f "$cpt" ] && tflag=(-t "$cpt")
  "$GMX" grompp -f "${mdp}.mdp" -o "${mdp}.tpr" -c "$cin" -r "$cin" \
    "${tflag[@]}" -p topol.top "${ndx[@]}" -maxwarn 5
  "$GMX" mdrun -deffnm "$mdp" -nb "$MDRUN_NB"
}

echo "== minimization =="
run_mdp step6.0_minimization step5_input.gro
prev=step6.0_minimization
for i in 1 2 3 4 5 6; do
  mdp="step6.${i}_equilibration"
  [ -f "${mdp}.mdp" ] || continue
  echo "== $mdp =="
  run_mdp "$mdp" "${prev}.gro" "${prev}.cpt"
  prev="$mdp"
done
echo "== equilibrated: $prev =="

NDX=(); [ -f index.ndx ] && NDX=(-n index.ndx)
echo "== grompp BASE production tpr (CHARMM-GUI step7, 2 fs) =="
"$GMX" grompp -f step7_production.mdp -o "${SYS}.tpr" -c "${prev}.gro" -t "${prev}.cpt" \
  -p topol.top "${NDX[@]}" -maxwarn 5
ATOMS=$("$GMX" dump -s "${SYS}.tpr" 2>/dev/null | grep -m1 "natoms" | grep -oE '[0-9]+' | head -1)
echo "== medium system: ${ATOMS} atoms =="

# Upload the BASE tpr now, before the HMR step -- the base medium system is the
# priority; HMR (D1) is a bonus. If ParmEd/HMR fails, we still keep the base.
echo "== upload BASE tpr =="
aws s3 cp "${SYS}.tpr" "$TPR_S3/${SYS}.tpr" --only-show-errors

# HMR is best-effort: a ParmEd hiccup on the CHARMM-GUI topology must not abort
# the build or lose the base tpr. Run the whole HMR block in a subshell that
# can fail without killing the script (set +e locally).
# HMR is NOT an mdp/grompp flag -- it rewrites atom masses (H x ~3-4, subtracted
# from the bonded heavy atom) so a 4 fs step is stable. The CHARMM-GUI topology
# already exists, so repartition it post-hoc with ParmEd's HMassRepartition (the
# standard tool). Best-effort: the whole block runs in a subshell that may fail
# without aborting the script or losing the already-uploaded base tpr.
echo "== HMR: repartition hydrogen masses (ParmEd), best-effort =="
if (
  set -e
  pip install --quiet parmed 2>/dev/null || sudo pip install --quiet parmed
  python3 <<'PY'
import parmed as pmd
top = pmd.load_file("topol.top", xyz=None)
pmd.tools.HMassRepartition(top).execute()   # default dmass=3.024, standard HMR
top.save("topol_hmr.top", overwrite=True)
print("wrote topol_hmr.top (H masses repartitioned)")
PY
  # Reuse step7_production.mdp verbatim except the timestep, so HMR and base
  # differ ONLY by dt + repartitioned masses -- a clean D1 comparison.
  sed -E 's/^([[:space:]]*dt[[:space:]]*=).*/\1 0.004/' step7_production.mdp > md-hmr-charmm.mdp
  grep -qiE '^[[:space:]]*dt' md-hmr-charmm.mdp || echo "dt = 0.004" >> md-hmr-charmm.mdp
  "$GMX" grompp -f md-hmr-charmm.mdp -o "${SYS}-hmr.tpr" -c "${prev}.gro" -t "${prev}.cpt" \
    -p topol_hmr.top "${NDX[@]}" -maxwarn 5
  aws s3 cp "${SYS}-hmr.tpr" "$TPR_S3/${SYS}-hmr.tpr" --only-show-errors
); then
  echo "== HMR tpr staged: ${SYS}-hmr.tpr =="
else
  echo "== WARN: HMR step failed (ParmEd/grompp) -- base tpr is staged; D1 HMR"
  echo "==       variant not produced. Base medium still usable. See log. =="
fi

echo "== done: ${SYS}.tpr (${ATOMS} atoms) staged =="

touch "$COMPLETION_FILE"
