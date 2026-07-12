# CellSegmentationEvaluator (CSE) integration — design

**Date:** 2026-07-12
**Branch:** `cellseg-evaluator-integration`
**Status:** approved for planning

## 1. Goal

Add reference-free **segmentation-quality scoring** to Mirage as an informational
QC step. After cells are segmented, score each patient's segmentation with the
Carnegie Mellon *CellSegmentationEvaluator* (CSE) metrics, and surface the
per-metric values plus the combined PCA `QualityScore` in the pipeline's QC
outputs.

Explicitly **out of scope** (chosen during brainstorming):

- No quality **gate** — a low score never fails or branches the pipeline.
- No **method selection** — we do not run multiple segmenters and pick a winner.
- **2D only** — Mirage processes 2D WSI, so we use CSE's `single_method_eval`
  (2D) path, not the 3D `CSE3D`/`single_method_eval_3D` path.

## 2. Background: what CSE is and why it was slow

CSE (`/Users/valer/Desktop/Github/CellSegmentationEvaluator-master`, v1.5.19)
computes ~14 reference-free metrics from a multichannel image + its cell/nucleus
mask, then collapses them to one `QualityScore` via a stored PCA model. The
usable surface is **SimpleCSE / the pip package**; the `full_pipeline` variant
pulls in MATLAB, DeepCell and HuBMAP downloads and is not viable inside Nextflow.

Two independent causes of slowness were identified:

1. **Sequential batch loop** — `SimpleCSE/seg_eval_main.py:55` iterates images in
   a plain Python `for` loop, one at a time. This vanishes the moment CSE runs as
   a per-patient Nextflow process (Nextflow's scheduler becomes the parallel loop).
2. **Un-vectorized inner math** — pure-Python per-cell and per-channel loops over
   NumPy, no compiled acceleration. Dominant hotspots (in
   `pip package/CellSegmentationEvaluator/functions.py`):
   - `get_matched_masks` (`:569`) — per-cell, per-pixel Python `map`/`set`
     construction; O(n_cells × pixels_per_cell).
   - `cell_type` / `cell_uniformity` (`:323`, `:374`) — per-channel × per-cell
     intensity aggregation via fancy-index + `np.sum`, replaceable by a single
     `scipy.ndimage` labeled reduction.
   - `silhouette_score` (`:435`) — O(n_cells²), computed for k=2..10, twice.

Tiling a single image is **not** a valid speedup: `cell_type` KMeans and
`get_quality_score` PCA are global over all cells, so per-tile scores would not
equal the whole-image score.

## 3. Integration point

`SEGMENT` runs **per reference image (≈ per patient)** inside
`subworkflows/local/postprocess.nf` (`:51-61`), driven by streaming
`groupTuple`. A new process placed here inherits patient-level parallelism for
free.

```
POSTPROCESSING (subworkflows/local/postprocess.nf)
  ch_references ── SEGMENT ──┬── ch_cell_mask   ──┐
                             └── ch_nuclei_mask ──┤
  ch_references ─────────────────────────────────┤ join on meta.patient_id
                                                  ▼
                                       SEG_QUALITY_EVAL (new)
                                                  ▼
                              *_seg_eval.json + versions.yml + *.size.csv
                                                  ▼
                              MERGE_SEG_EVAL (collect → segmentation_metrics.csv)
                                                  ▼
                              POSTPROCESSING.emit.seg_eval_metrics
                                                  ▼
                              GENERATE_QC_REPORT (workflows/mirage.nf)
```

The process is modeled on the existing `modules/local/seg_qc_geojson.nf`
(tag / label / container / `task.ext.when` gate / `versions.yml` heredoc /
`*.size.csv` trace / stub block), which is already a
"segment → emit per-image artifact + versions + size log" module.

## 4. Component breakdown

### 4.1 Vendored, patched CSE library — `bin/utils/cse/`

We **vendor** CSE's 2D metric code into the repo rather than `pip install` it,
because we are patching its internals and must keep that under version control.

- Import-only Python package; files stay git mode `100644` (per project
  convention for `bin/utils/*`).
- Preserve upstream `LICENSE` and the required citation in a `NOTICE`/module
  docstring.
- Call `single_method_eval` internals **directly** with explicit pixel sizes.
  This deliberately bypasses two upstream footguns:
  - `SimpleCSE/read_and_eval_seg.py:82-89` calls `input()` when pixel sizes are
    missing from OME metadata — would deadlock a headless job.
  - the pip `__init__.py` does not re-export the documented functions.
- Provide **two code paths** behind a flag:
  - `exact` — upstream algorithm, used as the equivalence oracle and as a
    fallback (`params.cse_fast = false`).
  - `fast` — vectorized, **bit-exact** rewrite (`params.cse_fast = true`,
    default). See §5.

### 4.2 Nextflow entry script — `bin/seg_quality_eval.py`

- Executable, git mode `100755` (name-invoked by Nextflow — see CLAUDE.md
  exec-bit rule).
- Args: `--cell-mask`, `--nuclei-mask`, `--image`, `--out`, `--pixel-size-um`
  (optional; falls back to reading the reference OME metadata), `--fast/--exact`,
  `--id`.
- Responsibilities:
  1. Read the two label TIFFs and **stack them into CSE's expected 2-channel
     mask** (channel 0 = cell, channel 1 = nucleus).
  2. Resolve pixel size (µm/px) from the reference OME metadata, else the
     `--pixel-size-um` param.
  3. Call the vendored `single_method_eval` (fast or exact).
  4. Write `<id>_seg_eval.json` (all metrics + `QualityScore`).

### 4.3 Process — `modules/local/seg_quality_eval.nf`

- `tag "${meta.patient_id}"`, `label 'process_high'` (tune during planning),
  `container` = the new `segeval` image (§4.5).
- Input: `tuple val(meta), path(cell_mask), path(nuclei_mask), path(image)`
  (the three channels joined on `meta.patient_id` in the subworkflow).
- Output: `tuple val(meta), path("*_seg_eval.json")` (metrics),
  `path "versions.yml"`, `path "*.size.csv"`.
- `when: task.ext.when == null || task.ext.when` gated by
  `!params.skip_seg_quality_eval`.
- Standard `versions.yml` heredoc + `*.size.csv` trace + `stub:` block.

### 4.4 Collect process — `modules/local/merge_seg_eval.nf`

Collects all per-patient JSONs into a single `segmentation_metrics.csv`
(replacing SimpleCSE's `seg_eval_main.py:64-68` combine step, which only ran in
directory mode). Emits the CSV for the QC report.

### 4.5 New container — `containers/segeval/`

Dedicated, fully pinned image (per project "one container per concern" +
immutable-tag conventions). Pinned deps: `numpy`, `scipy`, `pandas`,
`scikit-image`, `scikit-learn`, `aicsimageio` (+`[all]`), `tifffile`,
`xmltodict`. `scikit-learn` and `xmltodict` are the two deps no existing Mirage
container currently ships. Pushed to `ghcr.io/sceriff0/mirage/segeval:<tag>`;
documented in `containers/README.md`.

### 4.6 Wiring into QC aggregation — `workflows/mirage.nf` + subworkflow

Follow the existing `feature_distance_jsons` template
(per-image JSON → report input):

- `SEG_QUALITY_EVAL.out.versions.first()` → the subworkflow `ch_versions` mix
  (`postprocess.nf:333-345`); `.size_log` → `ch_size_logs` mix (`:312-325`).
  These propagate to the collated versions file and size-log aggregation
  automatically.
- Add `seg_eval_metrics` to `POSTPROCESSING`'s `emit:` (`postprocess.nf:352-356`).
- In `mirage.nf`, add a `ch_seg_eval_metrics = Channel.empty()` accumulator
  (near `:161-181`), mix the emit in, add a matching `path(...)` input to
  `modules/local/generate_qc_report.nf` (+ a `--flag` in its script), and pass
  `ch_seg_eval_metrics.collect().ifEmpty([])` at the `GENERATE_QC_REPORT(...)`
  call (`:188-195`).

### 4.7 Config, params, schema

- `conf/modules.config`: `withName: 'SEG_QUALITY_EVAL'` block using the
  memory-from-input-filesize idiom (as `SEG_QC_GEOJSON` at `:270-288`), `time`,
  and `publishDir` → `${params.outdir}/${meta.patient_id}/qc/segmentation`.
  `withName: 'MERGE_SEG_EVAL'` publishing the CSV to a QC location.
- New params (defaults in `nextflow.config`, documented in
  `nextflow_schema.json`):
  - `skip_seg_quality_eval` (bool, default `false`)
  - `cse_fast` (bool, default `true`)
  - `cse_pixel_size_um` (number, optional fallback when OME metadata lacks it)

## 5. The bit-exact speedup (Lever B)

Constraint chosen during brainstorming: the `fast` path must reproduce the
`exact` path's metrics **within 1e-6**. No subsampling, no approximation.
Permitted transformations (all value-preserving):

1. **Labeled reductions** — replace the per-channel × per-cell intensity loops in
   `cell_type`/`cell_uniformity` with `scipy.ndimage.mean` / `sum_labels` /
   `standard_deviation` over `labels=mask, index=cell_ids`. Same arithmetic,
   one C call instead of thousands of Python calls.
2. **Vectorized nucleus lookup** — replace the per-pixel Python `map` in
   `get_matched_masks` (`functions.py:588`) with NumPy fancy-indexing /
   `np.unique` over coordinate arrays. Same greedy, order-preserving assignment.
3. **Concurrent independent clustering** — the k=2..10 KMeans + silhouette passes
   are independent across `k`; run them concurrently (thread/process pool). This
   is exact — identical computation, merely parallel — and is the only lever
   available against the O(n_cells²) silhouette cost given no-subsampling.

**Residual cost:** with subsampling off, silhouette remains O(n_cells²) per `k`.
On extremely dense single images this is the dominant remaining cost; per-patient
fan-out covers the batch case regardless. Accepted trade-off.

## 6. Testing

- **Equivalence pytest** (the safety net for touching CSE's math):
  run `fast` vs upstream `CellSegmentationEvaluator==1.5.19` (pip, test-env only)
  on CSE's bundled `example_data/imgs/2D_CODEX.ome.tiff` + mask; assert every
  metric and `QualityScore` matches within `1e-6`. Lives under `tests/`,
  runnable via the project's `pytest` command, wired into CI.
- **nf-test stub** for `SEG_QUALITY_EVAL` and `MERGE_SEG_EVAL` under
  `tests/modules/`, added to the stub suite.
- Manual smoke: `nextflow run . -profile test,docker -stub` still green;
  a real-data run on one patient produces a non-empty `segmentation_metrics.csv`
  with a plausible `QualityScore`.

## 7. Risks / open items for planning

- **Pixel-size provenance** — confirm Mirage's registered reference OME-TIFFs
  carry physical pixel size; if not, `cse_pixel_size_um` becomes required for
  meaningful metrics (some are per-µm²).
- **Mask channel-order / matching semantics** — CSE truncates nuclei to cells and
  reports `FractionOfMatchedCellsAndNuclei`; verify Mirage's separate cell/nucleus
  label images share a consistent index space or that CSE's matching handles the
  mismatch as intended.
- **`single_method_eval` seg-channel branch** — upstream references an unset
  `img_thresholded` when OME metadata annotates `Nucleus`/`Cell` seg-channel
  names (`single_method_eval.py:96`); the vendored copy must fix this so it works
  whether or not seg channels are annotated.
- **Resource label** — `process_high` is a starting guess; right-size `cpus`/
  `memory`/`time` against a real dense WSI during planning.

## 8. Worktree

All work happens in the existing worktree
`/Users/valer/Desktop/Github/mirage-cellseg-wt` on branch
`cellseg-evaluator-integration` (branched from `main` @ `ae56666`).
