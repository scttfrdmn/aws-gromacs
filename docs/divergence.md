# Divergences from a "pure" FASRC/canonical benchmark set

Deliberate deviations, each with rationale. (Mirrors the `gauss/docs/divergence.md`
convention referenced in the parent CLAUDE.md.)

## The `medium` workload is a self-built system, not a canonical one

**What:** `small` (benchMEM, 82k) and `large` (benchRIB, 2M) are the canonical
Max Planck / MPINAT benchmark tprs. `medium` has no canonical equivalent — the
matrix's original `channel500k` was a placeholder name, not a real file. So we
**build** a real mid-size system from a deposited PDB (`preprocess/build_medium.sh`).

**Choices:**
- **System:** HEWL — hen egg-white lysozyme (PDB 1AKI), the canonical GROMACS
  tutorial structure — solvated in a large water box. Size (~medium) is dialed by
  box padding (`BOX_NM`, default 5.0 nm), not by the protein: protein-in-a-water-
  box is the standard MD benchmark shape, and a clean structure + big box is far
  more reliable to build unattended than a large crystal structure. (First tried
  β-galactosidase 6X1Q; it fails pdb2gmx on an incomplete residue — "Incomplete
  ring in HIS739" — the classic missing-atom problem of raw crystal PDBs. A clean
  protein + `-missing` tolerance is the robust automated path.)
- **Force field:** AMBER99SB-ILDN + TIP3P — both **bundled with GROMACS**, so the
  build needs no external download and is fully reproducible. Published, standard.
- **Protocol:** pdb2gmx (`-ignh -missing`) → dodecahedral box (`-d BOX_NM`) →
  solvate → 0.15 M NaCl → energy minimization → NVT (100 ps, 300 K, V-rescale,
  position-restrained) → NPT (100 ps, C-rescale barostat) → production. Real
  equilibration.
- **`atoms`** in `matrix.yaml` is set from the **measured** count the build
  reports, not guessed. Anywhere ~300k–800k qualifies as "medium"; the point is
  a real system between 82k and 2M, tuned via box size.

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
