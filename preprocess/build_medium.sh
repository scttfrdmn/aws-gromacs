#!/usr/bin/env bash
# Build the `medium` (~500k atom) benchmark system from a deposited PDB, fully
# from source, and produce base (2 fs) + HMR (4 fs) production .tpr files staged
# to S3. Runs ON a spawned instance inside the CUDA GROMACS image (gmx + the
# bundled AMBER99SB-ILDN force field). Equilibration is real (em/NVT/NPT), so the
# system is scientifically sound, not a toy.
#
# System: protein in a water box, sized to ~medium via box padding (BOX_NM).
# Default HEWL (hen egg-white lysozyme, PDB 1AKI) -- the canonical GROMACS
# tutorial structure, guaranteed to pdb2gmx cleanly. A large solvent box brings
# it to ~500k atoms (protein-in-water is the standard MD benchmark shape). Force
# field: AMBER99SB-ILDN + TIP3P, both bundled with GROMACS (reproducible).
# 0.15 M NaCl. NOTE: a raw large crystal structure (e.g. beta-gal 6X1Q) fails
# pdb2gmx on incomplete residues -- hence a clean protein + big box, not a big
# protein.
#
# Env in:
#   PDB_ID        deposited structure to fetch (default 1AKI, clean HEWL)
#   BOX_NM        solvent padding in nm (default 5.0 -> ~500k atoms; dials size)
#   TPR_S3        s3://<bucket>/gromacs-bench/tpr   (uploads <SYS>.tpr + <SYS>-hmr.tpr)
#   MDP_BASE_URL  raw URL prefix for the mdp files (this repo's preprocess/mdp)
#   SYS           output basename (default channel-medium)
#   MDRUN_NB      'gpu' (fast, needs a GPU host) or 'cpu' (default; runs anywhere,
#                 used when GPU capacity is unavailable). Equilibration only --
#                 does not affect the benchmark, just build wall-clock.
#   COMPLETION_FILE  sentinel for spawn --on-complete (touched on success only)
set -euo pipefail

PDB_ID="${PDB_ID:-1AKI}"   # HEWL, the canonical clean pdb2gmx structure
MDRUN_NB="${MDRUN_NB:-cpu}"
TPR_S3="${TPR_S3:?set TPR_S3}"
MDP_BASE_URL="${MDP_BASE_URL:?set MDP_BASE_URL}"
SYS="${SYS:-channel-medium}"
COMPLETION_FILE="${COMPLETION_FILE:-/tmp/SPAWN_COMPLETE}"
GMX="${GMX:-gmx}"
WORK="${WORK:-/tmp/build}"
mkdir -p "$WORK"; cd "$WORK"

# Push a build log to S3 on exit (success or failure) so the pipeline is
# debuggable from S3 without a live-instance race (same discipline as the cells).
LOG="$WORK/build_medium.log"
exec > >(tee -a "$LOG") 2>&1
trap 'aws s3 cp "$LOG" "$TPR_S3/build_medium.log" --only-show-errors 2>/dev/null || true' EXIT

echo "== fetch mdp files =="
mkdir -p mdp
for f in em nvt npt md md-hmr; do
  curl -fsSL "$MDP_BASE_URL/$f.mdp" -o "mdp/$f.mdp"
done

echo "== fetch PDB $PDB_ID =="
curl -fsSL "https://files.rcsb.org/download/${PDB_ID}.pdb" -o raw.pdb
# Keep only protein atoms (strip waters/ligands/hetatms the FF won't know).
grep '^ATOM' raw.pdb > protein.pdb || true

# System size is dialed by the solvent box padding (BOX_NM), NOT by needing a
# huge protein -- protein-in-a-water-box is the standard MD benchmark shape, and
# a clean, pdb2gmx-safe protein + a large box is far more reliable than a big
# crystal structure (which fails on missing/incomplete residues, e.g. 'Incomplete
# ring in HIS739' from a raw beta-gal entry). Default HEWL (1AKI) is THE canonical
# tutorial structure, guaranteed clean. Tune BOX_NM to hit ~medium atom count.
# Box padding sets the size. Measured: -d 5.0 around HEWL gave only ~24k atoms
# (dodecahedron volume grows slower than the naive cube estimate). ~500k needs a
# much larger box: volume scales ~linearly with atom count, so 500k/24k ~= 21x
# volume ~= 2.75x linear -> ~9 nm padding. Default 9.0; script reports the actual
# count so it can be tuned. (A big water box around one protein is a legitimate
# benchmark; the water IS the workload at this size.)
BOX_NM="${BOX_NM:-9.0}"

build_system() {  # $1 = topology dir tag, $2 = extra pdb2gmx flags (e.g. -heavyh)
  local tag="$1"; shift
  echo "== [$tag] pdb2gmx (AMBER99SB-ILDN + TIP3P) $* =="
  # 6 = AMBER99SB-ILDN, 1 = TIP3P, via stdin selections. -ignh regenerates H;
  # -missing tolerates incomplete residues in crystal structures (they get
  # rebuilt/ignored) so the automated build isn't derailed by one bad sidechain.
  printf '6\n1\n' | "$GMX" pdb2gmx -f protein.pdb -o "${tag}_proc.gro" \
    -p "${tag}.top" -i "${tag}_posre.itp" -ignh -missing "$@"
  echo "== [$tag] box + solvate (box padding ${BOX_NM} nm -> size target) =="
  "$GMX" editconf -f "${tag}_proc.gro" -o "${tag}_box.gro" -c -d "$BOX_NM" -bt dodecahedron
  "$GMX" solvate -cp "${tag}_box.gro" -cs spc216.gro -o "${tag}_solv.gro" -p "${tag}.top"
  echo "== [$tag] add ions (0.15 M NaCl, neutralize) =="
  "$GMX" grompp -f mdp/em.mdp -c "${tag}_solv.gro" -p "${tag}.top" -o "${tag}_ions.tpr" -maxwarn 2
  printf 'SOL\n' | "$GMX" genion -s "${tag}_ions.tpr" -o "${tag}_ions.gro" -p "${tag}.top" \
    -pname NA -nname CL -neutral -conc 0.15
}

equilibrate() {  # $1 = tag
  local tag="$1"
  echo "== [$tag] energy minimization =="
  "$GMX" grompp -f mdp/em.mdp -c "${tag}_ions.gro" -p "${tag}.top" -o "${tag}_em.tpr" -maxwarn 2
  "$GMX" mdrun -deffnm "${tag}_em" -nb "$MDRUN_NB"
  echo "== [$tag] NVT (100 ps, posres) =="
  "$GMX" grompp -f mdp/nvt.mdp -c "${tag}_em.gro" -r "${tag}_em.gro" -p "${tag}.top" \
    -o "${tag}_nvt.tpr" -maxwarn 2
  "$GMX" mdrun -deffnm "${tag}_nvt" -nb "$MDRUN_NB"
  echo "== [$tag] NPT (100 ps, posres) =="
  "$GMX" grompp -f mdp/npt.mdp -c "${tag}_nvt.gro" -r "${tag}_nvt.gro" -t "${tag}_nvt.cpt" \
    -p "${tag}.top" -o "${tag}_npt.tpr" -maxwarn 2
  "$GMX" mdrun -deffnm "${tag}_npt" -nb "$MDRUN_NB"
}

# --- base (2 fs) system ---
build_system base
equilibrate base
echo "== grompp base production tpr =="
"$GMX" grompp -f mdp/md.mdp -c base_npt.gro -t base_npt.cpt -p base.top \
  -o "${SYS}.tpr" -maxwarn 2
ATOMS=$("$GMX" dump -s "${SYS}.tpr" 2>/dev/null | grep -m1 "natoms" | grep -oE '[0-9]+' | head -1)
echo "== base system: ${ATOMS} atoms =="

# --- HMR (4 fs) system: same build with -heavyh, then the 4 fs production tpr ---
build_system hmr -heavyh
equilibrate hmr
echo "== grompp HMR production tpr (4 fs) =="
"$GMX" grompp -f mdp/md-hmr.mdp -c hmr_npt.gro -t hmr_npt.cpt -p hmr.top \
  -o "${SYS}-hmr.tpr" -maxwarn 2

echo "== upload tprs to S3 =="
aws s3 cp "${SYS}.tpr"     "$TPR_S3/${SYS}.tpr"     --only-show-errors
aws s3 cp "${SYS}-hmr.tpr" "$TPR_S3/${SYS}-hmr.tpr" --only-show-errors
echo "== done: ${SYS}.tpr (${ATOMS} atoms) + ${SYS}-hmr.tpr staged =="

touch "$COMPLETION_FILE"
