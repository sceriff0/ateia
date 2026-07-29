# Incremental cyclic-IF: `mode=add_cycle`

Fold a NEW imaging cycle into an already-completed patient run, reusing the prior
reference, segmentation mask, and old-marker quantification.

## Prerequisites
A previous run completed through postprocessing, producing under its `--outdir`:
`csv/registered.csv`, `csv/postprocessed.csv`, and per-patient
`segmentation/`, `quantification/`, `pyramid/` outputs.

**To be extendable incrementally, the prior run must have embedded its
segmentation masks in the pyramid** — i.e. it must have been run with
`--embed_masks true --quantify_compartments --expanded_quantification` (see
[Mask pyramid (`embed_masks`)](#mask-pyramid-embed_masks) below). If the prior
pyramid has no embedded mask series, `mode=add_cycle` **fast-fails** before
doing any work — see [Fast-fail behavior](#fast-fail-behavior).

## Run
```bash
nextflow run . -profile <profile> \
  --mode add_cycle \
  --prior_outdir results_cycle1 \
  --input new_cycle.csv \
  --outdir results_cycle2
```
- `--input`: same schema as a preprocessing start (`patient_id,path_to_file,is_reference,channels`),
  one row per new-cycle slide, `is_reference=false`, `DAPI` present.
- `--prior_outdir`: the previous run's `--outdir`.
- `--outdir`: a FRESH directory; the complete combined outputs (all markers,
  all cycles) are written here. The prior outdir is left intact.

## What is reused vs recomputed
- Reused: reference image, `*_cell_mask.tif` / `*_nuclei_mask.tif` (no SEGMENT),
  morphology/contours, prior marker columns.
- Recomputed: preprocess + register the new slide; quantify new markers; rebuild
  `cells.geojson` and the pyramid from the combined set.

## Marker collisions
A new-cycle marker that shares a name with a prior column overwrites it
(new cycle wins). `DAPI` is protected and never overwritten.

## Caveat
New-marker intensities are read through the cycle-1 mask, valid only if the new
cycle registers accurately. Check `--reg_qc 1` (DAPI overlay) QC per patient;
poor registration means the new markers for that patient are unreliable.

## Chaining cycles
Cycle N's `--outdir` becomes cycle N+1's `--prior_outdir`.

## Mask pyramid (`embed_masks`)
`params.embed_masks` (default `false`) controls whether `MERGE_AND_PYRAMID`
embeds the segmentation masks in the output pyramid OME-TIFF:

- When `embed_masks && quantify_compartments && expanded_quantification`, the
  process writes the cell + nuclei segmentation masks as a **second uint32
  image series** (`Image:1`) inside the pyramid, in addition to the normal
  intensity series (`Image:0`). This mask series is single full-resolution
  (labels are not averaged/downsampled across pyramid levels — that would
  corrupt label IDs) and is **never** added as extra channels of the intensity
  series: mixing a >65,535-cell uint32 label mask into the intensity channels
  would force the whole intensity image to uint32, which QuPath/Bio-Formats
  cannot open as a normal multi-channel image.
- When `embed_masks=false`, or `quantify_compartments`/`expanded_quantification`
  are off, `MERGE_AND_PYRAMID` produces a plain intensity-only pyramid — this
  is unchanged from behavior before mask embedding existed.

Cell objects are always delivered separately via `cells.geojson`; the embedded
mask series is an additional, optional way to carry the raw label masks —
`mode=add_cycle` is what actually consumes it.

## Fast-fail behavior
`mode=add_cycle` extracts the reusable cell/nuclei masks from the **prior**
pyramid's embedded mask series via `EXTRACT_MASK_SERIES`
(`bin/extract_mask_series.py`), rather than recomputing segmentation. If the
prior run's pyramid has fewer than two OME series (i.e. it was not produced
with `embed_masks=true` together with `quantify_compartments` and
`expanded_quantification`), extraction fails immediately with a clear error
identifying the missing mask series — before any registration or quantification
work runs on the new cycle. There is no silent fallback to re-segmentation.

## Cluster verification note
The mask-carrying pyramid's intensity series (`Image:0`) opens normally in
QuPath/Bio-Formats exactly like a standard pyramid — the embedded mask series
just rides along unopened for pipeline consumption (`EXTRACT_MASK_SERIES`).
This QuPath/Bio-Formats visual read of a mask-carrying pyramid should be
validated once on the cluster against a real run's output to confirm the
second series does not confuse the viewer's series/channel picker.
