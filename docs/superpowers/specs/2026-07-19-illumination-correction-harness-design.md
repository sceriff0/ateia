# Illumination-Correction Experiment Harness — Design

**Date:** 2026-07-19
**Branch:** `feature/illum-correction-compare`
**Status:** Implemented on branch `feature/illum-correction-compare` (Phase A complete; Phase B deferred)

## 1. Problem & Goal

The production preprocessing step (`bin/preprocess.py`, module `PREPROCESS`) applies
BaSiCpy shading correction. On this project's inputs — **already-stitched
multichannel mosaics** — BaSiC performs poorly because it is fed synthetic FOVs
sliced on an arbitrary grid (`fov_size=1950`) that do not align with the real
optical fields. It therefore models a smeared, grid-agnostic shading.

The user proposed a **periodic flat-field** approach that recovers the true tile
grid from the mosaic's periodicity and estimates one vignette per channel. The
goal of this work is **not** to blindly swap methods, but to **empirically find
the optimal correction for this project's data**, with:

- multiple plausible algorithm variants benchmarked against each other,
- resource/time efficiency (the naive apply peaks ~10 GB/channel),
- diagnostic plots for every step so the *why* is visible, and
- a report with stats + written recommendations for when variants look close.

"Optimal for my case" is data-dependent. Representative data lives on the
cluster and is not reachable in this environment, so the harness is built and
validated against **synthetic ground truth** and is designed to be run by the
user on the real mosaic.

## 2. Approach Overview

Two phases on one branch:

- **Phase A — offline experiment harness.** A standalone Python benchmark
  (outside Nextflow, for fast iteration) that runs every variant, captures QC +
  resource metrics, emits per-step diagnostic plots and a self-contained HTML
  report, **and produces a QuPath pyramid per variant** (so the user can also
  "look" at each candidate in QuPath). Validated on a synthetic mosaic with a
  known grid/vignette/darkfield/background.
- **Phase B — productionize the winner.** Once a winning variant is chosen,
  wire it into Nextflow as a compare subworkflow
  (`CONVERT_IMAGE → [BaSiC | winning-periodic] → SPLIT_CHANNELS →
  MERGE_AND_PYRAMID`) selectable via a dedicated `-entry`. Deferred and
  documented here; not built until a winner exists.

**Decision — pyramids in the harness:** rather than building an N-way variant
fan-out in Nextflow (throwaway), Phase A reuses the production pyramid writer
(`write_pyramidal_ome_tiff` in `bin/merge_channels_pyramid.py`) to emit a QuPath
pyramid per variant. This makes Phase A cover both "look at plots" and "look in
QuPath" for all variants, and guarantees the pyramids are byte-for-byte
comparable (identical writer) so any visual difference is attributable to the
correction, not the writer.

## 3. Design for Isolation — the three orthogonal axes

The proposed script tangles three independent operations. The harness separates
them so every combination can be toggled and scored:

1. **Flat-field / darkfield** (illumination):
   - `none` (identity; sanity baseline)
   - `basic` (existing BaSiCpy path; baseline to beat)
   - `periodic-int` (verbatim v1: integer pitch, no dark)
   - `periodic-float` (float-pitch tiling — fixes phase drift on large mosaics)
   - `periodic-float + const-dark` (constant pedestal subtracted before divide)
   - `periodic-float + varying-dark` (spatially-varying darkfield; stretch goal)
2. **Background removal** (non-periodic, additive diffuse signal):
   - `none`
   - `opening` (morphological grey-opening → zoom; the user's v2 method)
   - `gaussian` (large-sigma low-pass background estimate)
   - `tophat` / `rolling-ball` (skimage white top-hat / `rolling_ball`)
   - `median` (large-window median background)
3. **Smoothing:** `smooth_frac` sweep on the flat-field.

Default run uses a curated subset; `--full-grid` runs the whole matrix.
Nonsensical combinations (e.g. background removal on the `none` illumination
baseline is allowed as a control; `basic + background` is allowed to test BaSiC
plus background removal) are all expressible.

## 4. Library structure (`bin/illum/`)

Each module has one purpose, a documented interface, and is unit-testable in
isolation:

| File | Responsibility |
|---|---|
| `grid.py` | `recover_grid`, `period_from_profile`, `phase_from_profile`; downsampled recovery; **float-pitch** support; phase sanity checks |
| `flatfield.py` | `estimate_flatfield` (reduced-resolution estimate → upsample); `apply_field` (**row-chunked** to bound RAM); float-pitch tiling |
| `darkfield.py` | constant pedestal (`estimate_dark`) + spatially-varying darkfield |
| `background.py` | method registry: `opening`, `gaussian`, `tophat`/`rolling_ball`, `median` |
| `metrics.py` | `seam_peak`, `background_cv`, timing + peak-RSS capture |
| `pipeline.py` | `Variant` dataclass + `run_variant()` orchestration |
| `plots.py` | all diagnostic plots |
| `report.py` | self-contained HTML report, leaderboard, written recommendation |

**Entry scripts:**
- `bin/illum_correct.py` — thin single-variant CLI (pipeline-facing).
  Verbatim behavior by default; opt-in `--float-pitch`, `--dark auto|N|none`,
  `--background METHOD`. Pipeline-convention args (`--image --output_dir
  --channels --approx-tile`), threads pixel size from input OME metadata, writes
  `<stem>_periodic.ome.tif`. Set git mode `100755` (name-invoked bin script).
- `bin/illum_benchmark.py` — the sweep driver: runs all variants, writes plots,
  HTML report, metrics JSON, and a pyramid per variant.

## 5. Efficiency measures

- Grid recovery on a **downsampled** channel-sum image.
- Flat-field estimated at **reduced resolution** then upsampled (vignetting is
  smooth → ~16× less memory than stacking full-res tiles, negligible accuracy
  loss). Falls back to full-res if requested.
- **Row-chunked apply** so peak RAM is bounded (not a full float32 copy of a
  30k×30k channel + full field simultaneously).
- Channels processed **sequentially** (never a thread pool — that is what makes
  the naive path OOM at 4×10 GB).
- Per-variant **wall-time and peak-RSS** captured as first-class metrics, so
  "resource/time efficient" is measured, not asserted.

## 6. Diagnostic plots (per step → HTML report)

- Marginal row/col profiles.
- Autocorrelation curve with the chosen period marked.
- **Recovered-grid overlay** on a mosaic thumbnail (verify the grid locked).
- Per-channel flat-field heatmap.
- Folded-phase seam profile.
- Darkfield value/map and background estimate + residual.
- Before/after crops (a few tiles).
- **Seam-peak power spectrum before/after** (log-scale) at the tile frequency.
- Cross-variant bar charts: seam-suppression (X/Y), background CV, runtime, peak
  RSS.
- **Leaderboard table + written recommendation** explaining trade-offs when
  variants are close (e.g. "variant W wins seam suppression by X% at 1.4×
  BaSiC's runtime; background-CV ties, so prefer V if runtime dominates").

Ranking is a weighted composite (seam-suppression + background flatness, runtime
as tiebreak) **plus** the plots and narrative, because the user inspects results
visually and only falls back to stats when candidates look close.

## 7. Ground-truth validation & tests

`tests/testdata/generate_synthetic_mosaic.py` builds a multichannel mosaic from
tiles with a **known** vignette, constant darkfield, diffuse background, and grid
(pitch/phase), stitched with a configurable overlap blend. This:

- lets the report overlay *truth* on recovered estimates,
- drives `pytest` (grid recovered within tolerance; flat-field mean-1;
  correction recovers the injected field; each background method behaves;
  metrics improve post-correction),
- runs in CI without the cluster data.

`pytest` files: `tests/test_illum_grid.py`, `tests/test_illum_flatfield.py`,
`tests/test_illum_background.py`, `tests/test_illum_metrics.py`,
`tests/test_illum_pipeline.py`. Consistent with the repo's existing `tests/`
layout (`pytest -v tests/ --ignore=...`).

## 8. Phase B (deferred) — Nextflow productionization

Once a winner is chosen:

- `modules/local/illum_correct.nf` — `process_high_memory`, container with
  `scipy`/`tifffile`/`numpy` (reuse `merge` image if it carries scipy, else new
  `:illum` tag), standard template + `stub`, `size.csv`, `versions.yml`.
- `subworkflows/local/illum_compare.nf` — `CONVERT_IMAGE → [PREPROCESS |
  ILLUM_CORRECT] → SPLIT_CHANNELS → MERGE_AND_PYRAMID`, `meta.illum_method`
  tagging, publishDir disambiguation keyed on `meta.illum_method` (falls back to
  production path when absent → zero impact on `main`).
- `workflows/illum_compare.nf` + `-entry ILLUM_COMPARE` in `main.nf`
  (default `MIRAGE` entry untouched).
- Params: `illum_approx_tile=1950`, `illum_smooth_frac=0.12`,
  `illum_float_pitch=false`, `illum_darkfield=false`, `illum_background=none`.

### Contracts to satisfy before any eventual swap into `main`'s `PREPROCESS`
(out of scope now, noted for the promotion PR):
- Emit the `_dims.txt` sidecar consumed by registration prep.
- Preserve default DAPI-skip behavior (or make it a param).
- Match `*_corrected.ome.tif` naming if replacing in place.

## 8b. Metric hardening (2026-07-22) — un-gameable composite

A full-grid run on the real cluster mosaic exposed a metric flaw: the old composite
`seam_gain + cv_gain` ranked variants that drove the image toward **black** at the
top, because a zeroed image trivially has no seams and (with `background_cv`
recomputed on the corrected image) zero background variance. Grounded in a SOTA
review (`research/illumination-correction-sota-2026-07-22.md`; BaSiC/MCMICRO, SSIM,
QUAREP-LiMi, perception-distortion tradeoff), the metrics were reworked:

- **Fidelity gate.** `composite = artifact_score × fidelity`. Fidelity ∈ [0,1] is a
  no-reference signal-retention score (retained foreground dynamic range × foreground
  structure correlation) that collapses for a destroyed image — so over-subtraction
  can no longer win. Bounded (soft-squash) artifact terms replace the unbounded gains.
- **Offset-invariant flatness.** `background_flatness` (absolute std of a background
  ROI **fixed on the uncorrected image**) replaces CV in the ranking; CV is
  offset-sensitive (penalizes legitimate pedestal removal) and circular (thresholding
  the corrected image), so it is now diagnostic-only.
- **Full-reference validation.** `illum/metrics_reference.py` (SSIM/PSNR/RMSE +
  recovered-flatfield error) scores variants against the injected clean phantom;
  `tests/test_illum_antigaming.py` proves the new composite ranks a faithful
  correction above a zeroing one while the old composite did the opposite.
- **Instrumentation + plots.** peak-RSS now reports the absolute high-water mark (the
  old delta-vs-baseline was ~0 for every variant); before/after plots use the densest
  channel (DAPI) with a shared intensity scale; the redundant per-variant flat-field
  plot is deduped to one shared per-channel file. `cross_tile_uniformity` added as a
  downstream-relevant diagnostic (KASK 2016).

## 9. Non-goals

- Not replacing `PREPROCESS` on `main` in this branch.
- Not building an N-way Nextflow variant fan-out.
- Not a blind scalar optimizer; the human inspects, stats assist.

## 10. Algorithm critique carried into the design (why these variants exist)

- **Sub-pixel pitch discarded → phase drift.** `period_from_profile` returns a
  float, but estimation/apply round to int; over ~15–20 tiles a 0.3 px/tile
  error accumulates to 5–6 px and the field walks out of registration with the
  vignette. → `periodic-float` variant.
- **Overlap blend corrupts the estimate at seams** and is *coherent* across
  tiles, so `np.median(tiles)` does not remove it. Structural ceiling — measured
  by the seam-peak metric, not "fixable" by tuning.
- **Purely multiplicative model** mis-corrects an additive pedestal. → const- and
  varying-dark variants (subtract before divide).
- **Global median rescale** is fragile on IF data with large zero regions. Only
  applied in the no-dark cosmetic path; darkfield variants avoid it.
- **Diffuse autofluorescence** is a separate additive, non-periodic term →
  background-removal axis (multiple methods, on/off).
