# REDSEA lateral-spillover compensation

Corrects marker signal that leaks across the boundary between touching cells, so a
cell that merely *sits next to* a CD3+ T cell stops looking CD3+ itself.

Method: Bai, Zhu, Jiang *et al.*, "Adjacent Cell Marker Lateral Spillover
Compensation and Reinforcement for Multiplexed Images", *Front. Immunol.*
2021;12:652631 — <https://doi.org/10.3389/fimmu.2021.652631>. Reference
implementation: <https://github.com/nolanlab/REDSEA>.

Mirage's implementation is a reimplementation, not a translation, and one
parameter is chosen differently. Both are explained below.

## Quick start

```bash
nextflow run . -profile <profile> \
    --input samplesheet.csv --outdir results \
    --quantify_statistics Median,REDSEA \
    --redsea_markers CD3,CD8,CD20,CD4,CD68
```

That is the whole configuration. The boundary-band depth — the one parameter that
actually needs tuning — is calibrated from your own segmentation masks by default.

Then **read `<outdir>/<patient>/quantify/<patient>_redsea_qc.json` before trusting the
numbers.** It reports the band fraction that was achieved; see
[Choosing the band depth](#choosing-the-band-depth).

## What it adds

REDSEA is **purely additive**. It appends **one** whole-cell column per opted-in
marker and changes nothing that existed before:

| Column | Meaning |
|---|---|
| `<marker>: Cell: REDSEA` | Compensated integrated intensity ÷ cell area |

One column, not two, and specifically the **size-normalised** value: that is the
reference implementation's own recommendation. `github.com/nolanlab/REDSEA` writes
four FCS files and marks `dataRedSeaScaleSizeFCS.fcs` — "size-normalized
compensated counts" — as *(recommended)*. Emitting the raw compensated sum
alongside it would be a second column nobody is advised to use.

`REDSEA` is one of the base statistics `--quantify_statistics` accepts, alongside
`Median`, `Mean` and `Sum`. **Listing it is what turns compensation on** — there is
no separate boolean switch. It also takes the normalisation suffixes, so
`REDSEA Z` and `REDSEA RobustZ` standardise the compensated value across the
patient's cells.

These flow through `merge_quant_csvs` → `export_geojson` / `export_spatialdata`
unchanged, so they reach QuPath/FlowPath as ordinary measurements under the same
`"<marker>: <Compartment>: <Statistic>"` grammar. A consumer that does not know
the two new statistic names simply does not select them.

Three things it deliberately does **not** produce:

* **No compensated Median.** The compensation subtracts a fraction of a
  neighbour's *integrated* boundary counts. That algebra is defined on sums; a
  median has no equivalent. Mirage's default `Median` statistic is emitted
  un-compensated alongside. (A pixel-level variant *could* produce a compensated
  Median, but it cannot express REDSEA's reinforcement term — which has no
  pixel-level home, since the donated photons physically sit in the neighbour's
  pixels — so it would no longer be the published method.)
* **No per-compartment compensation.** REDSEA is a whole-cell membrane
  correction — there is no nucleus/cytoplasm decomposition of it.
* **No change to any existing column.** Adding `REDSEA` to
  `--quantify_statistics` cannot alter a number an earlier run produced.

> **Compensated sums are not on the raw sums' scale.** With the default
> `--redsea_checker 1` ("subtract and reinforce"), each cell is handed back the
> boundary signal it donated to its neighbours, so a genuinely positive cell's
> compensated sum is typically *larger* than its raw sum. Compare compensated to
> compensated; do not mix the two scales in one gate or one heatmap.

## Which markers

`--redsea_markers` is an opt-in list and has no default, because the method's own
assumptions restrict it:

> the signal is roughly uniform around the source cell's membrane, and is brighter
> inside the source cell than in the leak

That is a description of a **surface/membrane** marker. A nuclear marker (DAPI,
Ki67) or a diffuse cytoplasmic one violates it, and the result is
plausible-but-wrong numbers rather than an error. Name the surface markers
explicitly.

Matching is case-insensitive **exact** on the marker name — deliberately *not* the
substring rule `--nuclear_markers` uses, so `CD4` never silently selects `CD45`.

Listing `REDSEA` with an empty marker list is rejected up front: it
would compute the geometry for every patient and compensate nothing, producing
output byte-identical to `--redsea false` with no error anywhere.

## Choosing the band depth

REDSEA measures each cell's signal in a band of pixels just inside its own
boundary, and `elementSize` is that band's depth. It is the only parameter that
materially changes the answer.

### The paper's rule, and why it is not enough

The paper gives two calibration points and no formula:

| Modality | Mean cell area | Cell diameter | Published `elementSize` |
|---|---|---|---|
| MIBI-TOF (512 px / 400 µm → 0.781 µm/px) | 107 px | 11.7 px ≈ 9.1 µm | 2 px |
| CyCIF | 325 px | 20.3 px | 3–4 px |

and the instruction *"the pixel number for expansion should be proportional to
cell size"*. Both points sit at **≈17 % of the cell diameter**, which geometrically
means the band covers the outer **≈56 %** of a disk's area.

**Porting the literal `elementSize = 2` to this pipeline's optics would be badly
wrong.** At `--pixel_size 0.325` a 20 µm cell is 61.5 px across, so 2 px is 3 % of
its diameter — a sliver that misses nearly all of the membrane. The proportional
rule gives ≈11 px instead.

But the proportional rule is *disk* geometry, and real segmented cells are not
disks: an irregular or elongated cell has more perimeter per unit area, so the
same nominal depth swallows more of it. Measured on a Voronoi-like mask of 58 px
cells, the formula's 11 px produces a band fraction of **0.76**, not 0.56 — it
over-shoots by about a third.

### What Mirage does instead

Leave `--redsea_element_size` unset (the default) and `REDSEA_MATRIX` sweeps the
candidate depths **on your actual segmentation** and picks the one whose measured
band fraction is closest to `--redsea_target_band_fraction` (default `0.56`, the
paper's own operating point). The sweep is nearly free: the distance field is
computed once and each candidate is a threshold on it.

For the 58 px Voronoi mask above, calibration returns **7 px**, where the formula
said 11.

Pin `--redsea_element_size <n>` only when you want to reproduce a specific prior
run or compare against the published MATLAB.

### Reading the QC

`<outdir>/<patient>/quantify/<patient>_redsea_qc.json`:

```json
{
  "n_cells": 60,
  "n_neighbourless": 0,
  "element_size": 7,
  "element_shape": "disk",
  "band_fraction_mean": 0.549,
  "band_fraction_p90": 0.728,
  "cells_fully_band": 0,
  "element_size_calibrated": true,
  "recommended_element_size": 11,
  "band_fraction_by_element_size": { "1": 0.112, "2": 0.189, "...": "..." }
}
```

* `band_fraction_mean` is the number to check. Near **0.56** is the published
  operating point.
* **Too high (> 0.85)** — the band is nearly the whole cell, so "boundary mode"
  has silently become "whole-cell mode". The reference Python port's author
  reports exactly this ("cells that looked like they were ALL border pixels —
  which kind of ruins the point"). `REDSEA_MATRIX` logs a `[WARN]` for it.
* **Too low (< 0.15)** — barely any membrane signal is being compensated.
* `recommended_element_size` is the closed-form rule's answer, printed for
  comparison only. When it disagrees with `element_size`, the calibrated value is
  the one that was used, and it is the one to trust.
* `n_neighbourless` counts cells with no touching neighbour. Their signal passes
  through unchanged (correct: a cell with no neighbour receives no leak).

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `--quantify_statistics` | `[Median]` | Include `REDSEA` to enable compensation. |
| `--redsea_markers` | *(empty)* | Surface/membrane markers to compensate. Required when `REDSEA` is listed. |
| `--redsea_element_size` | `null` | Band depth in pixels. `null` = calibrate from the mask (**recommended**). |
| `--redsea_target_band_fraction` | `0.56` | Calibration target. |
| `--redsea_element_shape` | `disk` | Band metric. `disk` = Euclidean; `square`/`diamond` reproduce the original's `strel('square')`/`strel('diamond')`. |
| `--redsea_checker` | `1` | `REDSEAChecker`: `1` = subtract and reinforce (paper default), `0` = subtract only. |
| `--redsea_cell_diameter_um` | `20.0` | Advisory only — feeds `recommended_element_size` in the QC JSON. |

`--redsea_element_shape disk` is Mirage's default and is **not** one of the
original's two options. At the radii a fluorescence WSI needs, the original's
structuring elements are noticeably anisotropic — a `strel('square')` of radius 10
reaches 14.1 px along the diagonals and 10 px along the axes. `square` and
`diamond` remain available for fidelity comparisons.

## How it runs

REDSEA splits into a part that depends only on the mask and a part that depends
only on the channel, and the mixing in the compensation matrix is between *cells*,
never between channels. That separability is what lets it ride inside the existing
per-marker fan-out instead of becoming a new serial stage:

```
SEGMENT ──► cell_mask ──┬──► REDSEA_MATRIX          (once per patient)
                        │       │
                        │       └── geometry.npz ───┐
                        │                           │
                        └──► SPLIT_CHANNELS ──► QUANTIFY  (once per marker, in parallel)
```

`REDSEA_MATRIX` is one pass over the mask; the per-marker cost is one extra
`bincount` over the channel plus one sparse mat-vec whose non-zero count is about
six per cell.

### Measured cost

On a 4000 × 4000 tile (16 Mpx) of 6000 cells averaging 58 px across — the shape of
20 µm cells at `--pixel_size 0.325`:

| | |
|---|---|
| `REDSEA_MATRIX`, once per patient | **2.4 s** |
| Neighbours per cell (matrix sparsity) | **5.9** |
| Compensation matrix in RAM | **0.4 MB** |
| …as a dense matrix (the published MATLAB) | **0.3 GB** |
| Existing per-channel quantification | 1.12 s |
| **Extra per channel for REDSEA** | **0.23 s (+20 %)** |

Extrapolating to a 40 000 × 40 000 WSI (100× the area) with 40 markers:
`REDSEA_MATRIX` ≈ **4 min once per patient**, and ≈ **0.4 min extra per marker** —
which is 0.4 min of wall clock, not 16, because the marker tasks already run in
parallel. Against `MERGE_AND_PYRAMID`'s multi-hour tasks this is noise.

The sparsity is the whole story. At 600 000 cells the dense `cellPairMap` the
published MATLAB allocates would be **3 TB**; the sparse form is a few tens of MB.

### With `mode=add_cycle`

Incremental cyclic-IF goes through the same `QUANTIFY_MARKERS` subworkflow, so
`REDSEA` works there too and applies to the new cycle's markers. Note the
consequence: if the prior run had REDSEA off and the new cycle has it on, the
merged table carries REDSEA columns **only for the new cycle's markers** — the
prior markers' quantification is reused, not recomputed. To get compensated
columns for every marker, re-run the linear path rather than adding a cycle.

When `REDSEA` is absent from `--quantify_statistics`, `REDSEA_MATRIX` does not run and every `QUANTIFY` task is
handed `assets/NO_REDSEA` — a placeholder that keeps the process's input arity
fixed rather than making `QUANTIFY` two different processes depending on a
parameter.

## Differences from the published implementation

The reference is MATLAB
(`MIBIboundary_compensation_boundarySA.m`). Mirage reimplements it because the
published code cannot run at WSI scale, and fixes two things along the way.

**1. Dense → sparse.** The original allocates `cellPairMap = zeros(cellNum,
cellNum)`. That is ~30 MB for a MIBI field of view and **2 TB at 5 × 10⁵ cells**.
A cell touches about six neighbours, so the matrix is intrinsically sparse;
`scipy.sparse` removes the quadratic term entirely. The scalar per-pixel and
per-cell `for` loops are vectorised for the same reason.

`tests/test_redsea.py` includes a literal transcription of the published MATLAB
and asserts the fast path reproduces it exactly on a walled mask, for both
`REDSEAChecker` values.

**2. Touching-label masks.** The original finds neighbours by scanning pixels
labelled `0`, which assumes a one-pixel zero wall between cells — the output of a
classic watershed. Every segmenter this pipeline ships (StarDist, CellSAM,
InstantSeg; likewise Mesmer/DeepCell) emits cells that **abut directly, with no
wall**. Run literally against such a mask, every cell's perimeter comes out as
zero, the normalisation divides by it, and NaN propagates through the mat-mul to
every cell in the image. Mirage counts both conventions and adds them, so walled
and touching masks — and mixtures — all work.

**3. Isolated cells no longer NaN the image.** The original's
`cellPairMap ./ cellBoundaryTotalMatrix` has no zero guard, so a single cell with
no neighbour puts NaN in a row and the mat-mul spreads it everywhere. Here such
rows are left at zero, which passes that cell's signal through unchanged.

**4. The boundary band is defined directly.** MATLAB dilates the zero pixels by a
structuring element and intersects with each cell — only equivalent to "within
`elementSize` of *this cell's own* edge" when a zero wall exists. Mirage marks
each cell's own inner rim and takes everything within `elementSize - 1` of it under
the chosen metric. Identical to the original at small radii on a walled mask, and
well-defined on a touching mask where the original is not.

## Limitations (the paper's own)

* No 3D correction.
* Cannot separate physically overlapping cells.
* Does not address autofluorescence, spectral bleed-through, or isotopic
  contamination — it is a *spatial* correction only.
* Performance depends on segmentation quality. A mask that merges two cells has
  no boundary between them for REDSEA to compensate across.
