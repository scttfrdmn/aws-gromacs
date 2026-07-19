# Divergences from a "pure" FASRC/canonical benchmark set

Deliberate deviations, each with rationale. (Mirrors the `gauss/docs/divergence.md`
convention referenced in the parent CLAUDE.md.)

## The `medium` workload is a self-built system, not a canonical one

**What:** `small` (benchMEM, 82k) and `large` (benchRIB, 2M) are the canonical
Max Planck / MPINAT benchmark tprs. `medium` has no canonical equivalent — the
matrix's original `channel500k` was a placeholder name, not a real file. So we
**build** a real mid-size system from a deposited PDB (`preprocess/build_medium.sh`).

**Choices:**
- **System:** a real **membrane protein in a POPC bilayer** — the same *class* as
  benchMEM (82k), scaled to ~medium. Built with **CHARMM-GUI Membrane Builder**
  (the field-standard tool), which embeds the protein in the bilayer, solvates,
  ionizes (0.15 M KCl), and exports **GROMACS inputs + CHARMM36m** directly.
- **Force field:** CHARMM36m (as CHARMM-GUI ships it) — note this differs from
  the AMBER/GROMACS-SIMD arms; the point of `medium` is a real mid-size membrane
  system, and it carries its own consistent FF. Recorded here as the deviation.
- **Ingestion:** `preprocess/build_medium_charmmgui.sh` runs CHARMM-GUI's own
  equilibration chain (minimization → step6.1–6.6 graduated-restraint equilibration)
  verbatim, then grompps the base (2 fs, CHARMM-GUI step7) and HMR (4 fs) production
  tprs. HMR masses are repartitioned with **ParmEd `HMassRepartition`** on the
  CHARMM-GUI topology (there is no gmx/grompp flag for HMR).
- **`atoms`** in `matrix.yaml` is set from the **measured** count the build
  reports. Sized in CHARMM-GUI (bilayer patch) to land ~300–500k.

**Why CHARMM-GUI and not a scripted build:** a correct atomistic membrane system
is genuinely hard to assemble unattended — membrane insertion + FF correctness is
the error-prone core of membrane MD. Earlier attempts confirmed this: raw crystal
PDBs fail pdb2gmx on incomplete residues (β-gal 6X1Q, "Incomplete ring in
HIS739"), and a plain protein-in-water box is not a recognizable membrane system.
The deposited CHARMM-GUI archive for SERCA (1SU4) exists but is CHARMM-only (no
full-system topology; needs licensed CHARMM to assemble). So the real path is to
let CHARMM-GUI build it and export GROMACS, then ingest that. (The AMBER
`build_medium.sh` remains for a soluble-protein build but is not the medium path.)

**Why it matters beyond size:** the distributed benchMEM/benchRIB tprs use
`all-bonds` constraints, which preclude the GPU-resident update path
(`-update gpu`; recorded `infeasible:fit` — see docs/findings.md). The built
system uses `h-bonds` constraints, so **gpu-resident works on it** — making
`medium` the workload that can actually demonstrate true GPU-resident placement,
and the one carrying a real **HMR variant** (`-heavyh`, 4 fs) for the D1
"software beats hardware" demo.

## GPU image SIMD is AVX2_256, not AVX-512

The single CUDA image runs on all GPU host CPUs, which vary — g6/g6e are AMD
EPYC 7R13 (Zen3, no AVX-512). An AVX-512 build SIGILLs there. Host SIMD is minor
for GPU cells (compute is offloaded), so AVX2_256 is the safe common denominator.
(See docs/gromacs-delivery.md.)

## HMR (D1) runs on `medium` only

The distributed benchMEM/benchRIB ship no source (`.mdp`/`.gro`/`.top`), and HMR
cannot be derived from a `.tpr` (`convert-tpr` can't edit masses/dt). So the HMR
variants exist only for the self-built `medium` system, where we control the
topology. The D1 demo is therefore measured on `medium`.
