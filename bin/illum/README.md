# illum — illumination-correction experiment library

Benchmark flat-field / darkfield / background-removal variants on a stitched mosaic.

## Input

Accepts a **`.nd2`** (Nikon) or **`.tif`/`.tiff`** mosaic. For `.nd2`, channel
names and pixel size are read from the file metadata, so `--channels` is
optional; for a plain TIFF without OME channel names, pass `--channels`.

Dependencies: `numpy scipy tifffile matplotlib` (required); optional
`scikit-image` (adds the `rolling_ball` background method), `basicpy` (adds the
`baseline-basic` BaSiC anchor under `--full-grid`), and **`nd2`** (required only
for `.nd2` input — `pip install nd2`).

## Usage

- Sequential run:
  `illum_benchmark.py --image mosaic.nd2 --outdir bench --approx-tile 1950 --full-grid`
  → `bench/report.html`, `bench/metrics.json`, `bench/plots/`, `bench/pyramids/`.
- `--full-grid` runs the full variant matrix (leads with `baseline-uncorrected`
  and `baseline-basic` visual anchors); `--no-pyramids` skips pyramid writing;
  `--max-channels N` limits channels for a quick pass; `--channels A B ...`
  overrides metadata names.
- `illum_correct.py` applies ONE variant (pipeline-facing), writing `<stem>_periodic.ome.tif`.

## Parallel (SLURM) — see `slurm/submit_illum_grid.sh`

One variant per array task, then a dependent aggregate job:
- `illum_benchmark.py --list-variants --full-grid --outdir DIR`  → variant names, one per line
- `illum_benchmark.py --variant NAME --image mosaic.nd2 --outdir DIR ...`  → `DIR/parts/NAME.json` + pyramid + plots
- `illum_benchmark.py --aggregate --outdir DIR`  → `DIR/metrics.json` + `DIR/report.html`

Run on the real cluster mosaic, open `report.html`, and compare pyramids in QuPath.

## Ranking — the composite score

`composite = artifact_score × fidelity`, both bounded, so it **cannot be gamed by
over-subtraction** (a black image has no seams and zero background variance, so
artifact metrics alone would rank "zero the image" first — see
`research/illumination-correction-sota-2026-07-22.md`):

- **artifact_score** ∈ (−1, 1): mean of soft-bounded seam-suppression and
  background-flatness gains. Flatness uses `background_flatness` (absolute std of a
  background ROI **fixed on the uncorrected image**) — offset-invariant, so
  removing a dark pedestal is not penalized the way CV would. CV is still reported
  as a diagnostic but is not ranked on.
- **fidelity** ∈ [0, 1]: no-reference signal retention (geometric mean of retained
  foreground dynamic range × foreground structure correlation). It **multiplies**
  the artifact score, so a variant that suppresses seams by eating real signal —
  low fidelity — scores ~0 instead of winning.

Full-reference SSIM/RMSE (`illum/metrics_reference.py`) validate the no-reference
ranking on the synthetic phantom (`tests/test_illum_antigaming.py`). Diagnostic
plots use the **densest channel** (DAPI) with a shared Before/After intensity scale
so signal loss is visible. When composites are within ~0.01, decide from the crops
and QuPath.
