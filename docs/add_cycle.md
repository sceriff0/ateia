# Incremental cyclic-IF: `mode=add_cycle`

Fold a NEW imaging cycle into an already-completed patient run, reusing the prior
reference, segmentation mask, and old-marker quantification.

## Prerequisites
A previous run completed through postprocessing, producing under its `--outdir`:
`csv/registered.csv`, `csv/postprocessed.csv`, and per-patient
`segmentation/`, `quantification/`, `pyramid/` outputs.

**To be extendable incrementally, the prior run must have embedded its
segmentation masks in the pyramid** — i.e. it must have been run with
`embed_masks = true` with `quantify_compartments` and `expanded_quantification` (see
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
  one row per new-cycle slide, `is_reference=false`, a `params.nuclear_markers`
  channel present (default `DAPI`/`CELLTOX`, matched case-insensitively).
- `--prior_outdir`: the previous run's `--outdir`.
- `--outdir`: a FRESH directory; the complete combined outputs (all markers,
  all cycles) are written here. The prior outdir is left intact.
- `--start`/`--stop` **do not apply in this mode and are rejected at launch.**
  `add_cycle` runs a fixed path — preprocess the new cycle, register it against
  the frozen prior reference, quantify, export — there is no partial-run choice
  to make. Passing `--stop` at all, or `--start` with anything other than its
  own default (`preprocessing`), is a launch-time error.

## What is reused vs recomputed
- Reused: reference image, prior marker columns (the prior run's merged
  quantification table, joined onto the new-cycle CSVs by cell label), and the
  cell + nuclei segmentation masks (no SEGMENT re-run) -- but NOT read back from
  the prior run's `<pid>/segmentation/` files. They are re-extracted from the
  **prior pyramid's embedded `Image:1` mask series** via `EXTRACT_MASK_SERIES`
  (see [Fast-fail behavior](#fast-fail-behavior)), which is why the prior run
  must have been produced with `embed_masks=true`.
- Recomputed: preprocess + register the new slide; cell contours (and nucleus
  contours, under `--quantify_compartments`) via `EXTRACT_CELL_PROPERTIES` /
  `EXTRACT_NUCLEI_PROPERTIES` against the reused masks -- contours are NOT
  reused from the prior run, only the masks they're recomputed from are;
  quantify new markers; rebuild `cells.geojson` and the pyramid from the
  combined set.

## Marker collisions
A new-cycle marker that shares a name with a prior column overwrites it
(new cycle wins). The nuclear/fiducial marker (`params.nuclear_markers`,
default `DAPI`/`CELLTOX`) is protected and never overwritten.

## Caveat
New-marker intensities are read through the cycle-1 mask, valid only if the new
cycle registers accurately. Check `--reg_qc 1` (DAPI overlay) QC per patient;
poor registration means the new markers for that patient are unreliable. At
`--reg_qc 2`, the per-cell registration-residual CSVs (`SEG_QC`'s staged
seg-overlap QC) are included in the QC report the same way they are on the
standard path -- an earlier version of `add_cycle` computed them but never
surfaced them anywhere.

**Known limitation:** a new cycle contributing more than one slide for the same
patient currently fails during registration QC (`--reg_qc >= 2`, the shipped
default) -- `SEG_QC` cross-multiplies each moving slide against every
transform registered for that patient, producing duplicate-named QC files that
collide when the report stages them. `--reg_qc 0` avoids it; a single slide
per patient per cycle is otherwise unaffected.

## Chaining cycles
**Not yet supported.** `--prior_outdir` must contain BOTH `csv/registered.csv` and
`csv/postprocessed.csv` (`ParamUtils.validateAddCycle`). An add_cycle run now writes
the first, but it has no postprocessing step -- masks and the base quantification
table are reused rather than re-derived -- so it writes no `csv/postprocessed.csv`
and cycle N+1 fails its launch validation. Each add_cycle run must currently take
its `--prior_outdir` from a full linear run.

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
- When `embed_masks=false` (with `quantify_compartments`/`expanded_quantification`
  set however you like), `MERGE_AND_PYRAMID` produces a plain intensity-only
  pyramid — this is unchanged from behavior before mask embedding existed.
- `embed_masks=true` REQUIRES both `quantify_compartments=true` and
  `expanded_quantification=true` — `ParamUtils.validateCompartmentQuant` now
  rejects `embed_masks=true` with either sibling off at launch, rather than
  silently falling back to a plain pyramid. (Before this check existed, that
  combination exited `0` with no mask series, discovered only later when the
  pyramid was handed to `mode=add_cycle` as `--prior_outdir` — see "Fast-fail
  behavior" below.)

Cell objects are always delivered separately via `cells.geojson`; the embedded
mask series is an additional, optional way to carry the raw label masks —
`mode=add_cycle` is what actually consumes it.

## Fast-fail behavior
`mode=add_cycle` extracts the reusable cell/nuclei masks from the **prior**
pyramid's embedded mask series via `EXTRACT_MASK_SERIES`
(`bin/extract_mask_series.py`), rather than recomputing segmentation. If the
prior run's pyramid has fewer than two OME series (i.e. the prior run used
`embed_masks=false`, the only way to reach that state now that
`ParamUtils.validateCompartmentQuant` rejects `embed_masks=true` with either
`quantify_compartments` or `expanded_quantification` off at launch — see "Mask
pyramid" above), extraction fails immediately with a clear error identifying
the missing mask series — before any registration or quantification work runs
on the new cycle. There is no silent fallback to re-segmentation.

## Cluster verification note
The mask-carrying pyramid's intensity series (`Image:0`) opens normally in
QuPath/Bio-Formats exactly like a standard pyramid — the embedded mask series
just rides along unopened for pipeline consumption (`EXTRACT_MASK_SERIES`).
This QuPath/Bio-Formats visual read of a mask-carrying pyramid should be
validated once on the cluster against a real run's output to confirm the
second series does not confuse the viewer's series/channel picker.
