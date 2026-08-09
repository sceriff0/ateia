# Changelog

All notable changes to the MIRAGE pipeline will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **A new `segmentation` step, carved out of `postprocessing`.** `SEGMENT`,
  `EXTRACT_CELL_PROPERTIES` and `EXTRACT_NUCLEI_PROPERTIES` (the latter under
  `--quantify_compartments`) moved from `subworkflows/local/postprocess.nf` into their
  own `subworkflows/local/segmentation.nf`, with a resumable checkpoint
  (`Layout.SEGMENTED` / `Checkpoint.columns('segmented')`). `--start`/`--stop` now
  accept `segmentation` between `registration` and `postprocessing`
  (`ParamUtils.STEP_ORDER` and `nextflow_schema.json`'s `start`/`stop` enums both
  updated). Only `nucleus_contours` is recorded as the empty string when
  `--quantify_compartments` is false (see `lib/Checkpoint.groovy`'s EMPTY VALUES
  note) — `nuclei_mask` is always populated, since `SEGMENT` always produces it and
  `postprocess.nf`'s mask join depends on that being true unconditionally. The
  checkpoint schema stays fixed across that param setting either way.
  `--start postprocessing`'s samplesheet must now additionally carry `cell_mask` and
  `nuclei_mask` columns (`ParamUtils.STEPS`'s `postprocessing` entry — both are
  dereferenced unconditionally by `segmentation.nf`'s `READ_SEGMENTED_CHECKPOINT`),
  so a plain `registered.csv` fails validation loudly instead of dying deep inside
  `segmentation.nf` with "Argument of `file` function cannot be null".
  **Published-output change:** `csv/segmented.csv` is a new published file, one row
  per registered slide. `csv/preprocessed.csv`, `csv/registered.csv` and
  `csv/postprocessed.csv` are byte-identical to before this change. At
  `--start postprocessing`, `csv/postprocessed.csv`'s `cell_mask` column now
  correctly names a path under the PRIOR run's `--outdir` (wherever segmentation
  actually ran), not this run's — a new cross-outdir dependency in that manifest,
  unlike every other column in it.
- **`lib/Layout.groovy` gained `publishedOrAsIs`/`isUnderTaskDir`.** A channel that can
  carry either a fresh task-directory output (this run) or an already-published path
  read back from an earlier checkpoint (`--start segmentation`'s `INPUT_CHECK`,
  `--start postprocessing`'s `READ_SEGMENTED_CHECKPOINT`) needs `Layout.publishedPath`
  applied only in the first case — in the second, the file is already correct and
  reconstructing it against this run's `--outdir` mis-records it (wrong outdir, and
  for a one-level producer subdirectory like `registered_slides/`, potentially the
  wrong kind too). `isUnderTaskDir` checks the immediate parent and grandparent,
  which covers every producer-subdirectory depth this pipeline's `publishDir` blocks
  use. `Layout.passthroughPath` now delegates to `publishedOrAsIs` rather than
  duplicating the same check.
- **`lib/Checkpoint.groovy`** — the checkpoint CSV's column list, header and row builder
  now have one owner. The three writers ask for the header instead of restating it, and
  `Checkpoint.row()` orders values by the declared column list and throws on a missing or
  unknown column. No published header, column or path changes.
- **`tests/lib_probe.nf`** — a unit-test surface for `lib/*.groovy`. nf-test's assertion
  context cannot see `lib/` classes, so the assertions live in a pipeline script, run by
  CI's `nextflow-stub` job and by `tests/lib_probe.nf.test`.
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
- **`reg_tiled_fanout` now defaults to `true`.** The per-tile fan-out
  (`TILED_COARSE` → `TILED_REG_TILE` ×tiles → `TILED_SOLVE` → `TILED_STITCH`) is the only
  STARE execution shape whose memory is actually bounded: every process' peak is a
  function of a parameter — `reg_tiled_coarse_max_dim`, `reg_tiled_tile` +
  `reg_tiled_halo`, `reg_tiled_out_tile` — rather than of the slide's dimensions.
  Measured peak RSS across the whole chain on a 16384² 2-channel tiled OME-TIFF:
  **0.91 / 1.31 / <1.31 / 1.35 GB**, against an 8 GB budget.

  The old default, `false`, runs a single `TILED_REGISTER` task per slide. That path is
  *not* region-streamed despite its name: `bin/tiled_register.py` holds the whole
  reference stack, the whole moving stack, a float32 all-channel copy (`mov_hwc`) and
  the full-size warped output simultaneously, and `tiled_pipeline.register_slide`
  additionally materialises a whole-slide float64 `mov_rigid`. It remains available for
  users who would rather have a handful of large tasks than hundreds of small ones, and
  its budget is now derived from input size, but it is no longer what you get by
  default.

  **Two consequences worth knowing.** Scheduler load rises sharply: at the default
  `reg_tiled_tile = 2048` a 26k² slide plans ~169 `TILED_REG_TILE` tasks *per moving
  slide* (capped at `maxForks = 20`), where before it was one task. And nf-test cases
  that select `registration_method = 'tiled'` without pinning `reg_tiled_fanout` now
  exercise the fan-out chain rather than `TILED_REGISTER`; the two produce an identical
  manifest and identical pixels, but `TILED_REGISTER` consequently loses its incidental
  default coverage — `tests/checkpoint_manifest.nf.test` pins the fan-out path
  explicitly, and the single-task path now needs a test that pins
  `reg_tiled_fanout = false` if it is to stay covered.

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
- **`params/full_pipeline.json` now sets `"seg_method": "stardist"`.** The file already set
  `segmentation_model`, `segmentation_model_dir`, `seg_n_tiles_x`, `seg_n_tiles_y`, `seg_pmin`
  and `seg_pmax` — all StarDist-only flags — but `nextflow.config` defaults `seg_method` to
  `instantseg`, so a run launched with `-params-file params/full_pipeline.json` silently
  discarded all six values and segmented with InstanSeg instead. Launching from this preset
  now runs StarDist: a different backend, container, and set of `versions.yml`/mask outputs
  than before this fix. Anyone relying on the file's prior (InstanSeg) behaviour must now pass
  `--seg_method instantseg` explicitly.
- **`<outdir>/size_logs/versions.yml`'s single key changed** to
  `MIRAGE:FINAL_QC:AGGREGATE_SIZE_LOGS` (diagnostic string only; no functional effect).
- **`--quantify_compartments`/`--expanded_quantification`/`--embed_masks` are now
  resolved once (`ParamUtils.compartmentMode`) and threaded down as an argument**,
  the same seam `--registration_method` already has, replacing a ternary that used to
  be copied verbatim between `postprocess.nf` and `add_cycle.nf` plus several
  independent raw reads across `segmentation.nf`/`postprocess.nf`/`add_cycle.nf`/
  `assemble_export.nf`. No published-output change — behaviour verified
  byte-identical across the full stub scenario matrix.
  **Behaviour change: `ParamUtils.validateCompartmentQuant` now also rejects
  `--embed_masks true` unless BOTH `--quantify_compartments` and
  `--expanded_quantification` are also true.** Previously `--embed_masks true
  --quantify_compartments false` (or with `--expanded_quantification false`) exited
  `0` and silently published a pyramid OME-TIFF with **no** mask series
  (`assemble_export.nf`'s `embed_masks` gate is `embed_masks && quantify_compartments
  && expanded_quantification`), a gap only discovered later when that `--outdir` was
  handed to `mode='add_cycle'` as `--prior_outdir` and `EXTRACT_MASK_SERIES` found no
  `Image:1` to reuse. A launch that used to succeed under that combination now fails
  fast with a clear error instead. **To recover:** either drop `--embed_masks` (if you
  do not need the mask series in the pyramid), or set both
  `--quantify_compartments true --expanded_quantification true` alongside it.
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
- **`add_cycle`-only: `SPLIT_PRIOR_PYRAMID` now publishes to its own leaf.**
  **Published-output change:** `<outdir>/<patient_id>/split_channels/prior/<marker>.tiff`
  is new; nothing at `<outdir>/<patient_id>/split_channels/<marker>.tiff` moves.
  `subworkflows/local/add_cycle.nf` includes `SPLIT_CHANNELS` a second time as
  `SPLIT_PRIOR_PYRAMID` (re-splitting the prior run's pyramid so its channels can be
  deduped against the new cycle's). Nextflow's `withName:` selector matches an alias
  against both its own name and the process' original declared name, so
  `withName: 'SPLIT_CHANNELS'` was routing **both** invocations to the same
  `<pid>/split_channels/` directory — whichever finished last silently overwrote the
  other's file for any marker name the two shared. This is a **published-artifact**
  collision only: the in-memory channel deduplication `add_cycle.nf` already does
  before building the pyramid (new-cycle channel wins, `DAPI` always protected — see
  `docs/add_cycle.md`'s Marker collisions section) reads each task's staged output
  directly and was never affected. Pre-existing since `add_cycle.nf:40` first aliased
  the include; not introduced by this branch.
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
- **Every module's `versions.yml` (`script:` and `stub:`) now renders from one tool list**
  (`lib/ProcessEnvelope.groovy`) instead of two hand-written, independently-maintained
  heredocs. Touches 26 of 27 `modules/local/*.nf` files (`segment.nf` is the one documented
  exception — see its `ProcessEnvelope` comment). No published `versions.yml` content
  changes on a stub run; on a **real** run, the same two modules — `extract_mask_series.nf`
  and `merge_and_pyramid.nf` — pick up two deltas each: they now probe `python` instead of
  `python3` for their version rows (both run on an image where `python` exists; `python3`
  was inconsistent authoring, not a deliberate choice), and their probes now fail soft
  (`|| echo "unknown"`) instead of erroring the task if a tool's import fails — a fallback
  their hand-written `python3` heredocs never had.
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
- **`TILED_COARSE` OOM-killed (exit 137) on real slides — the STARE anchor was never
  the thumbnail step the design specified.** `docs/parallel_registration_design.md`
  has always listed `COARSE  thumbnail feature-align (ORB + RANSAC) -> M0  ~1-2 GB`,
  and the "8 GB is generous" sanity-check only ever analysed `REG_TILE`/`WARP_TILE`/
  `STITCH`. `bin/tiled_coarse.py` implemented no thumbnail: it called
  `dapi_channel(load_channels(path))` — the eager whole-slide `tifffile.imread` that
  `tests/test_tiled_reg_tile_lazy.py` had already retired from the per-tile step, and
  the last caller of it — then ran ORB at native resolution, where
  `coarse_align._orb_features` promotes to float64 and scikit-image stacks a Gaussian
  pyramid plus FAST/Harris responses on top. Measured at ~40 bytes per source pixel:
  ~35 GB on a 26k x 26k slide, against `8.GB * task.attempt`, which is how one patient
  burned all four attempts and aborted the run. Both slides are now read through
  `open_lazy` in byte-budgeted row bands (`tiled_io.read_decimated`, 64 MB/band) and
  decimated by **one shared factor** (`tiled_io.decimation_factor` — a per-slide factor
  would put the two thumbnails at different scales and bake a bogus scale term into
  M0), so peak is ~2 GB regardless of slide size. `M0` and its residual TRE are lifted
  back to full-resolution pixels via `coarse_align.scale_transform_to_full_res`;
  `ref_h`/`ref_w` stay full-resolution so the tile plan still spans the frame. New
  `params.reg_tiled_coarse_max_dim` (default 4096 px, 0 disables) is the cost/accuracy
  knob. `TILED_COARSE`'s retry ramp now **doubles** (8/16/32/64 GB) instead of scaling
  linearly — a slide that overshoots an 8 GB estimate overshoots it by multiples, so
  the old 8/16/24/32 ramp spent every attempt and still died. Guarded by
  `tests/test_tiled_coarse_thumbnail.py` (8 cases, each watched failing against a
  targeted mutation) and `tests/modules/tiled_coarse.nf.test`'s rendered-command case,
  which asserts `--max-dim` reaches the command line — `-stub` cannot see a `script:`
  block, so nothing else could catch that flag being dropped.
- **`TILED_REGISTER`'s memory budget was the fan-out budget applied to a
  non-fan-out process.** Its `withName:` comment claimed it was "tile-streamed"; it is
  not. `bin/tiled_register.py` holds the whole reference stack, the whole moving stack,
  a float32 all-channel `mov_hwc` copy and the full-size warped output at once, and
  `tiled_pipeline.register_slide` additionally promotes both DAPI planes to float64 and
  materialises a whole-slide `mov_rigid` — ~10-15x the decompressed moving stack, not
  8 GB. The budget is now derived from input size (`~10 GB per GB on disk`, the
  `PREPROCESS` pattern). This is a *derived* figure, not a measured one: the genuinely
  memory-bounded path is `--reg_tiled_fanout true`, which is now correct end to end.
- **The `tiled` image now installs `procps`, which every task in it needed to start at
  all.** `containers/tiled/Dockerfile` is `FROM python:3.11-slim` and had no `apt-get`
  layer, so it shipped without `ps`. Because `params.enable_trace` defaults to `true`,
  Nextflow injects its task-metrics wrapper into every task, and that wrapper's first
  line is `command -v ps &>/dev/null || { >&2 echo "Command 'ps' required by nextflow to
  collect task metrics cannot be found"; exit 1; }` — it runs *before* the `script:`
  block. Every `registration_method='tiled'` run therefore died at the first
  `TILED_COARSE` task with **exit status 1, empty stdout and no traceback**, which reads
  like a silent crash in `tiled_coarse.py` rather than a missing system package. The
  Dockerfile now installs `procps` and asserts `ps -e -o pid= -o ppid=` at build time, so
  a regression fails the image build instead of an HPC run. `containers/README.md` gains
  a "Runtime requirements every image must satisfy" section stating the invariant. No
  other image is affected: `preprocess` and `spatialdata` already installed `procps`, and
  the remaining CUDA/PyTorch/TensorFlow-derived bases carry it — all of them are proven
  by prior end-to-end runs, which ran under the same always-on trace wrapper.
  **Operational note:** the descriptive tag `bolt3x/attend_image_analysis:tiled` is
  mutable and was re-pushed in place, so cluster-side Singularity caches must be
  refreshed (delete the local `.img`/`.sif` and re-pull) before the fix takes effect.
- **`EXTRACT_NUCLEI_PROPERTIES` now writes its outputs into a `nuclei/` subdirectory of
  its own task directory** (`modules/local/extract_nuclei_properties.nf`), instead of
  the task directory root. The published location is unchanged
  (`<outdir>/<pid>/cell_properties/nuclei/*`, via `conf/modules.config`'s default
  subdirectory-preserving `publishDir`, no `path:`-only override) — this only fixes a
  latent gap where `Layout.publishedPath(outdir, pid, 'cell_properties', file)` could
  not recover the `nuclei/` segment from the file's own task-directory structure (it
  only saw the config's hardcoded path suffix), which the new `segmented` checkpoint
  (above) needs in order to record `nucleus_contours`' real published path rather than
  colliding with `contours`' path. `EXTRACT_NUCLEI_PROPERTIES`'s own size-log CSV now
  publishes to `<outdir>/<pid>/cell_properties/` instead of `.../cell_properties/nuclei/`
  (a diagnostic-only artifact, not read by any checkpoint or downstream consumer).
- **The per-cell registration residuals now reach the QC report on the standard
  (linear) path, and, as of this change, on `add_cycle` too.** `SEG_QC.out.per_cell`
  was emitted through `REGISTRATION.out.seg_residuals` and routed to the SpatialData
  export, but never tagged into the artifact stream, so no report ever showed it. It
  is now the artifact kind `seg_residuals`, rendered in the report as a capped table
  (first 500 rows per CSV; `bin/generate_qc_report.py`'s `SEG_RESIDUALS_MAX_ROWS`).
  `add_cycle.nf` calls `SEG_QC` too and, until now, never captured `.out.per_cell` at
  all — that gap is closed in the same change that wires it into
  `workflows/mirage.nf`'s `add_cycle` `FINAL_QC` call site.
  **`GENERATE_QC_REPORT`'s input arity changed from 7 to 8** (a new
  `seg_residuals`/`path(..., stageAs: 'seg_residuals/*')` slot) — any external caller
  of that module directly must add the new argument. `FINAL_QC` additionally fails the
  run, on the default path (`--skip_final_qc_report=false` and `--enable_trace=true`,
  both shipped defaults), if any declared artifact kind has no consumer — previously
  such a kind was silently dropped.
- **`--mode add_cycle` now rejects `--start`/`--stop` outright, instead of accepting
  and silently ignoring them**, matching `docs/add_cycle.md`. It used to validate its
  own prerequisites and return before the linear step gate ever ran, so a
  contradictory pair like `--start postprocessing --stop preprocessing` reported
  "validations passed" under `--dry_run` instead of the hard error the standard path
  always gave for the same pair — and a value like `--stop registration` ran the
  ENTIRE fixed path through export while `run_summary.json`'s `stop` label claimed
  the run had stopped after registration. `ParamUtils.validateStop` and the
  `shouldRun` gate computation now run once, before the `params.mode == 'add_cycle'`
  branch, so both paths share the ordering check; `add_cycle` itself then calls the
  new `ParamUtils.validateAddCycleStepFlags`, which throws unless `--stop` is left
  unset and `--start` is left at its default `preprocessing`.
  **Behaviour change: a launch that used to succeed with a non-default `--start`/
  `--stop` under `--mode add_cycle` now fails fast instead.** `add_cycle` runs a
  FIXED path (new-cycle samplesheet -> preprocess -> register against the frozen
  prior reference -> quantify -> export) — there is no `--start`/`--stop` choice to
  make. **To recover:** omit both flags. **Also fixed:** `add_cycle`'s `FINAL_QC`
  call used to pass the literal string `'add_cycle'` as the run summary's `stop`
  label — not a member of `ParamUtils.STEP_ORDER`, so any future consumer indexing
  it would get `-1`. It now unconditionally passes `ParamUtils.STEP_ORDER.last()`
  (`'postprocessing'`), not `effective_stop` — the rejection above guarantees the
  fixed path always runs through export, so that is the only honest label.
- **`add_cycle.nf`'s pyramid-channel grouping now carries the same
  `groupKey(patient_id, channels_count)` + `remainder: true` streaming hint as
  `postprocess.nf`'s.** It had drifted to a bare `.groupTuple()` with no size hint at
  all. The grouping itself — group tagged `[patient_id, channels_count, tiff]` triples
  into one per-patient list and build the patient-level meta — is now
  `groupTiffsByPatient`, a shared function in `subworkflows/local/quantify_markers.nf`
  (a plain function, not a process/workflow, imported via the same `include` syntax).
  `add_cycle`'s combined channel count is `new_count + prior_count`, deliberately not
  correcting for a marker-name collision: an over-count is safe here (the group still
  closes complete, just via `remainder: true` instead of exactly on time), while an
  under-count is the failure mode that aborts `MERGE_AND_PYRAMID` outright (see
  `groupTiffsByPatient`'s doc comment). No published-output change: verified via a
  before/after stub-run tree diff (43 files both times, differing only in the
  QC report's timestamped filename).
- **`tests/test_register.py` could never fail.** It imported `merge_first_file`, which
  `bin/register.py` does not define, behind a `pytest.importorskip("valis")` that meant
  the `ImportError` was never reached.
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
