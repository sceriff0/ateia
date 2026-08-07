# Changelog

All notable changes to the MIRAGE pipeline will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`nuclear_markers`** parameter (default `['DAPI', 'CELLTOX']`) — an ordered preference list
  naming the nuclear/fiducial channel. The first marker present (resolved from channel metadata,
  never the filename) is moved to channel 0 by `CONVERT_IMAGE`. Fails fast if none is present
  (single-channel images excepted).
- **An `--mode add_cycle` run now writes `csv/registered.csv` into its `--outdir`.** It never did
  before — only the linear `REGISTRATION` path wrote that checkpoint, so an `add_cycle` run's
  `--outdir` could never itself serve as a second `add_cycle`'s `--prior_outdir`. Chaining two
  `add_cycle` runs is still **not** supported: `csv/postprocessed.csv` is still not written
  (`add_cycle` has no `POSTPROCESSING` step), and `ParamUtils.validateAddCycle` requires both
  checkpoints to be present.

### Changed
- **The registration method's intrinsic TRE is no longer named after VALIS.** Both backends
  estimate a target registration error from their own registration — VALIS a feature-distance
  `*_summary.csv`, STARE a `*_tre.json` — but the QC pipeline called the slot `valis_summary`
  end to end. It is now `registration_tre`: the adapters emit `intrinsic_tre`, `REGISTRATION`
  emits `registration_tre`, and that is the artifact kind `FINAL_QC` recognises.
  **Published-output change:** the folder inside the `qc/mirage_qc_data_*/` report bundle that
  holds these raw artifacts is renamed `valis_summary/` -> `registration_tre/`. Nothing else
  about the published tree changes — no path, filename or checkpoint-CSV column moves.
  `GENERATE_QC_REPORT`'s `--valis-summary` flag is likewise now `--registration-tre` (an
  internal process-to-script interface; the parameter surface is unchanged).
- **`nuclear_markers` is now honoured by every consumer**, not only `CONVERT_IMAGE`:
  `SPLIT_CHANNELS`/`split_multichannel.py`, `SEGMENT`, `SEG_QC_GEOJSON`, and `CsvUtils`'
  samplesheet validation and channel counting all resolve the nuclear/fiducial channel from the
  same rule now, so it genuinely drives both cell segmentation and the registration fiducial.
  **Behaviour change under the shipped default `['DAPI', 'CELLTOX']`:** a samplesheet with a
  channel whose name *contains* `CELLTOX` (case-insensitive) now has that channel dropped from
  non-reference slides, exactly as `DAPI` always was. On an affected run, `add_cycle`'s
  double-written `merged_quant.csv` becomes a single correct file, and the redundant per-cycle
  fiducial disappears from `*_quant.csv`. Users who want the old behaviour should set
  `--nuclear_markers DAPI`.
- **`--nuclear_markers` now accepts a comma/space-separated string as well as a list**, so
  `--nuclear_markers CELLTOX` works on the command line (`nextflow_schema.json` type widened to
  `["array", "string"]`).
- **`SEG_QC_GEOJSON` (the `reg_qc=2` GeoJSON registration-QC path) now passes
  `--tolerance ${simplify_tolerance}` to `segment_to_geojson.py`.** Previously it passed no
  tolerance at all, so its contours were simplified at the script's own default (`0.5`) while
  `EXTRACT_CELL_PROPERTIES` — the GeoJSON `WARP_SEG_QC` compares this one against — used the
  configured `simplify_tolerance` (`1.0`). The two GeoJSONs `WARP_SEG_QC` diffs were therefore
  simplified at different tolerances. **Behaviour change:** `reg_qc=2` registration-QC scores
  (dice/displacement) from before this change are **not comparable** to scores computed after
  it, on any run using the GeoJSON path. Also factored the StarDist flags SEGMENT and
  SEG_QC_GEOJSON both need (model, tiling, cell-expansion distance, pmin/pmax, prob-threshold)
  into one shared definition (`conf/modules.config`'s `starDistCommonFlags()`), which had
  drifted apart into two hand-built copies; `--nuclear-markers` stays built in
  `seg_qc_geojson.nf`'s script block (unchanged behaviour — see that file's comment for why it
  could not move alongside the rest). **`--expand-distance` is now wired to
  `params.seg_expand_distance` for `SEG_QC_GEOJSON` too** (it previously always used
  `segment_to_geojson.py`'s Python default, `10`, regardless of `--seg_expand_distance`) —
  behaviourally a no-op under the shipped default (both are `10`), but an operator overriding
  `--seg_expand_distance` now gets consistent expansion between `SEGMENT` and `SEG_QC_GEOJSON`
  instead of the same "compared at different settings" defect `--tolerance` had.
- **`reg_micro_reg` now defaults to `2`** (maximum: micro-rigid + micro non-rigid), previously `0`.
  Registrations run the full micro-registration by default — a quality-over-speed change
  (micro-registration can add ~30–120 min per registration).
- **`--seg_method` with an unrecognised value now fails fast** instead of silently running
  StarDist.
- **`<outdir>/size_logs/versions.yml`'s single key changed** to
  `MIRAGE:FINAL_QC:AGGREGATE_SIZE_LOGS` (diagnostic string only; no functional effect).
- **`SPLIT_CHANNELS` and `QUANTIFY` now publish per patient.**
  **Published-output change:** `<outdir>/split_channels/<marker>.tiff` moves to
  `<outdir>/<patient_id>/split_channels/<marker>.tiff`, and
  `<outdir>/quantify/<patient>_<marker>_quant.csv` moves to
  `<outdir>/<patient_id>/quantify/`. Both processes previously had no `publishDir` of
  their own and inherited a run-level, patient-unaware default. Because
  `split_multichannel.py` names its outputs by sanitised channel name alone, **two
  patients sharing a marker published to the same path and the second silently
  overwrote the first** on every multi-patient run. Nothing in the pipeline reads the
  published copies, so the run stayed green and the loss was invisible. No checkpoint
  CSV column, no other published path, and no GeoJSON measurement key changes.
- **`EXTRACT_MASK_SERIES` (the `--mode add_cycle` mask-reuse step) now publishes per
  patient too.** **Published-output change:** `<outdir>/extract_mask_series/cell_mask.tif`
  and `.../nuclei_mask.tif` move to `<outdir>/<patient_id>/segmentation/`. This process
  had the identical unprefixed-filename defect `SPLIT_CHANNELS` had: on a two-patient
  `add_cycle` run it published exactly two files total — one `cell_mask.tif`, one
  `nuclei_mask.tif` — for both patients combined, the second silently overwriting the
  first. It shares the `segmentation` leaf with `SEGMENT`'s own masks; this is safe
  because `--mode add_cycle` requires a fresh `--outdir` distinct from `--prior_outdir`
  (`ParamUtils.validateAddCycle`), so `<pid>/segmentation/` is never occupied by anything
  else in that run.
- **The fall-through `publishDir` default is now a guard.** A process with no explicit
  `publishDir` publishes to `<outdir>/_UNROUTED_PUBLISH/<process>` instead of to a
  plausible-looking run-level directory, and `tests/test_layout.py` fails the build if
  any process reaches it.
  **Published-output change:** `<outdir>/tiled_coarse/` and `<outdir>/tiled_reg_tile/`
  (the STARE tiled-registration fan-out's per-tile anchor JSON, tile-plan CSV, and
  per-tile control-point JSON — `reg_tiled_fanout=true` only) are no longer published at
  all; both processes were already relying on the same unrouted default this change
  closes off, and their content is purely intermediate — `TILED_SOLVE` consumes it and
  is the one that publishes the durable artifacts: the registration manifest, and the
  `*_tre.json` whose `"tiles"` records (see `bin/utils/tre_report.py`) carry the same
  per-tile diagnostic information forward. Both got an explicit
  `publishDir = [ enabled: false ]` so this is a declared decision, not a silent gap.
- **`WARP_SEG_QC_TILED` is merged into `WARP_SEG_QC`.** One process now serves both
  registration backends, with container, flags, stage list, version rows and stub extras
  coming from `lib/WarpBackends.groovy` — the same shape `SEGMENT` already uses for its
  three segmentation backends. The two processes shared ~77% of their bodies.
  **`<outdir>/size_logs/input_sizes.csv` row change (not a new published file):** the
  tiled path's `process` column now reads `WARP_SEG_QC` instead of
  `WARP_SEG_QC_TILED` — the per-task `<prefix>.*.size.csv` this aggregates from is
  never itself published (`WARP_SEG_QC`'s `publishDir` pattern is `*_seg_qc.json`
  only). The `*_seg_qc.json` and `*_reg_residuals.csv` filenames, contents and
  locations are unchanged. `params.registration_method` is still read exactly once
  for DISPATCH, in `subworkflows/local/registration.nf`; `WARP_SEG_QC`'s
  `--jvm-heap-gb 8` (VALIS-only) is resolved from the `method` process input via
  `lib/WarpBackends.groovy`, not from the param, so the two can never disagree.

### Fixed
- **The checkpoint manifests no longer name files that do not exist.** `csv/postprocessed.csv`'s
  `cell_csv` and `cell_geojson` columns previously recorded `<pid>/geojson/cells.geojson` — a
  basename-only path that omitted the `export/` subdirectory `EXPORT_GEOJSON`'s `publishDir`
  actually carries the file into, so the recorded path pointed at nothing. All published paths
  now go through `lib/Layout.groovy`, which preserves the producer subdirectory, so the columns
  name the real files. **Published-output change (explicitly authorised):** the two path values
  in `csv/postprocessed.csv` change; the published tree itself does not.
- **The three checkpoint CSVs (`preprocessed.csv`, `registered.csv`, `postprocessed.csv`) now
  have deterministic row order.** `collectFile` wrote rows in task-completion order, so two runs
  of the same commit could produce differently-ordered (though content-identical) CSVs. Rows are
  now sorted (`sort: true`, patient ID then path). **Published-output change (explicitly
  authorised):** row order in the three manifests changes; contents and column order do not.

### Removed
- **`reg_reference_markers`** — removed. It only fed a filename-based fallback for reference-image
  selection that the pipeline never reached (the reference slide is chosen by `is_reference`, and
  `--reference` is always passed to VALIS). Nuclear-channel selection is now `nuclear_markers`
  (metadata-based). `register.py`'s standalone `--reference-markers` CLI flag is unaffected.
- **`padding` / `pad_mode`** and the `PAD_IMAGES`, `GET_IMAGE_DIMS`, and `MAX_DIM` processes —
  removed. Padding was default-off and unused: both registration backends align inputs of differing
  sizes natively (VALIS into a shared space; the tiled/STARE backend into the reference's shape).
  This also removed the pipeline's only filename-based channel-name inference.

## [1.0.0] - 2026-07-29

First public release. End-to-end multiplex WSI processing: preprocessing, registration,
segmentation, per-cell quantification, optional phenotyping, and QuPath-compatible
GeoJSON + pyramidal OME-TIFF export.

### Added
- **Preprocessing** — BaSiC illumination correction with FOV tiling; Bio-Formats/ND2 →
  OME-TIFF conversion; multi-channel parallel processing.
- **Registration** — two backends selected by `--registration_method`:
  - `valis` (default): VALIS graph-based whole-slide alignment, with optional staged
    micro-registration (`reg_micro_reg` = 0/1/2).
  - `tiled`: STARE — JVM-free, internally tiled, fully parallel registration
    (`reg_tiled_*` params), usable on a laptop.
- **Staged registration QC** (`reg_qc` = 0/1/2) — DAPI overlay plus segmentation-overlap
  dice/displacement attributed per registration stage.
- **Segmentation** — three backends via `--seg_method`: StarDist (default), InstanSeg,
  and CellSAM; GPU or CPU; configurable nuclei→whole-cell expansion.
- **Quantification** — per-cell morphology and per-channel intensity; optional
  per-compartment (Nucleus / Cytoplasm / Cell) signal and expanded Mean/Sum statistics.
- **Phenotyping (optional, panel-agnostic)** — conformal-risk-controlled cell-type
  assignment when a panel is supplied (`panel_spec` / `panel_model`); skipped by default.
- **Reference-free segmentation-quality evaluation** — CellSegmentationEvaluator
  `QualityScore` with a `cse_max_pixels` downsample cap.
- **Export** — QuPath-compatible `cells.geojson` and pyramidal OME-TIFF; configurable
  contour simplification (`simplify_tolerance`) and coordinate precision.
- **Incremental cyclic-IF** (`--mode add_cycle`) — fold a new imaging cycle into a
  completed patient run, reusing the prior reference, segmentation mask, and old-marker
  quantification; recomputes only the new cycle.

### Infrastructure
- DSL2 Nextflow architecture with step-based execution (`--start` / `--stop`).
- Checkpoint-based restart (preprocessing, registration, postprocessing).
- SLURM/Singularity and Docker execution profiles.
- Per-process version tracking, aggregated QC report, and computational-resource report.
- JSON-schema parameter definitions plus Groovy validation utilities.

[1.0.0]: https://github.com/sceriff0/mirage/releases/tag/v1.0.0
