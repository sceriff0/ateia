# Changelog

All notable changes to the MIRAGE pipeline will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Migration — read before comparing any output across this release

Seven changes alter what a run produces or what its outputs MEAN, and every one of
them is silent from the consumer's side. The first six are collected, each with what
changed and what to do about it, in
[`docs/migration-2026-08-24.md`](docs/migration-2026-08-24.md); the seventh landed
after that doc and is detailed inline below:

1. **Darkfield correction is gone** — nf-core `BASICPY` runs at
   `get_darkfield=False`; the deleted in-house path used `True`. Corrected pixel
   values are not comparable across this release.
2. **NaN measurements are omitted, not written as `0.0`** — a cell with no
   nuclear overlap carries fewer keys than its neighbours. This does NOT crash
   `qupath-extension-flowpath` (its lookup null-checks) and does not change which
   cells a gate calls positive; what moves is every percentile-derived threshold,
   because the artificial zeros have left the distribution.
3. **STARE meshes differ, by design** — two new control-point gates reject points
   the previous code accepted. Registration output is not bit-comparable.
4. **Checkpoint CSVs carry an `id` column** — an existing `--prior_outdir`, or an
   existing tree used with `--start`, now fails loudly at launch instead of
   silently re-deriving an identity that may differ.
5. **A run publishes final outputs only and deletes its work directory** —
   `cleanup_level` defaults to `'final'` and `cleanup_work` to `true`. No
   checkpoint manifest is written at a cleaning level. Pass
   `--cleanup_level none` on any run whose output will be re-entered.
6. **Two invocations that used to be accepted now fail at launch** —
   `--seg_instantseg_target cells|nuclei` (they produce one mask, which was
   silently replicated into both outputs and zeroed every cytoplasmic
   measurement), and an OME header whose `PhysicalSizeXUnit` the ASHLAR retiler
   does not recognise (a recognised non-µm unit is now converted rather than read
   as µm — an `nm` header was a 1000× scale error).
7. **`pixel_size` now defaults to `'auto'`** — it was `null`, and `ParamUtils`
   forced the operator to assert a scale before a run could start at all. `'auto'`
   reads each image's own OME `PhysicalSizeX`, so a run that previously refused to
   start at launch may now proceed — and will use the FILE's scale, which may
   differ from whatever the operator would have typed. A new `PREFLIGHT_SCALE`
   step resolves (and, for `auto`, verifies) every input slide's scale before any
   heavy work starts: it hard-fails when `auto` cannot resolve a slide (absent or
   uninterpretable OME metadata), and warns — never fails — when a supplied number
   disagrees with the file, cannot be confirmed against it, or when slides in one
   run resolve to genuinely different scales. Every checkpoint manifest now also
   carries a `pixel_size` column; a checkpoint written by an earlier version is
   REFUSED on re-entry with a "this checkpoint predates scale tracking" error — the
   remedy is to re-run the step that wrote it. Measurements are unchanged for any
   run that already passed an explicit `--pixel_size`.

### Added
- **`PREFLIGHT_SCALE` process** (`modules/local/preflight_scale.nf` /
  `bin/preflight_scale.py`), run once over every input slide before any heavy work
  is staged (`subworkflows/local/input_check.nf`). It reads only OME metadata —
  never pixel data — resolves what `--pixel_size` will actually use for each
  slide, and records the result into `meta.pixel_size` so downstream consumers
  that have no image of their own to read a scale from (`EXPORT_GEOJSON`,
  `EXPORT_SPATIALDATA`, `SEG_QUALITY_EVAL`, InstantSeg) no longer see the literal
  unresolved string `'auto'`. Fails loudly, naming every offending slide, when
  `auto` cannot resolve one; warns, naming both numbers, when a supplied number
  disagrees with a file's own metadata or cannot be confirmed against it; and
  warns, naming every distinct value and the slides carrying it, when slides in
  one run resolve to genuinely different scales — a legitimate mixed-magnification
  cohort is never refused, only surfaced. See the migration note above.
- **`reg_tiled_frontend` parameter** (default `'orb'`, STARE/`registration_method=tiled` only) —
  selects COARSE's global-alignment front-end: `orb` (default, today's behaviour, unchanged),
  `sift` and `fourier_mellin` (zero-new-dependency CPU alternatives), or `disk_lightglue` (the
  learned DISK+LightGlue matcher — see below). Wired through
  `bin/utils/coarse_align.py::estimate_affine` and rendered into `TILED_COARSE`'s command by
  `conf/modules.config`. **Resume note:** adding `--frontend` to `TILED_COARSE`'s rendered
  command changes that task's hash, so the *first* `-resume` after upgrading past this change
  re-runs STARE's whole COARSE-and-downstream registration chain even for runs that never touch
  `--reg_tiled_frontend` — a one-time cost, not a per-run one.
- **`torch` + `kornia` and the DISK/LightGlue weights ship inside `bolt3x/mirage-tiled`.**
  `--reg_tiled_frontend disk_lightglue` needs no profile, no second image and no download at
  first use: `containers/tiled` installs `torch==2.3.1` (CPU wheel, from
  `download.pytorch.org/whl/cpu`) and `kornia==0.7.3`, and BAKES both pretrained checkpoints
  (`depth-save.pth`, `disk_lightglue_v0-1_arxiv-pth`) into `TORCH_HOME=/opt/torch` at build
  time, world-readable. That matters because kornia otherwise fetches them from the network on
  FIRST USE into `$HOME/.cache/torch` — and the target cluster has a read-only `$HOME` and is
  documented as air-gapped (`docs/usage.md`), so the first real run would have failed the same
  way the VALIS JVM cache did. This is a container-only fix; a `git pull` cannot repair a
  missing checkpoint. The image keeps its JVM-free/libvips-free/CUDA-free property; it is no
  longer torch-free, and that earlier leanness claim is deliberately retired.
  An interim development iteration put torch+kornia in a SEPARATE optional
  `bolt3x/mirage-stare-ml` image selected by a `stare_ml` profile. Both are **removed**, and
  neither ever shipped: `.github/workflows/build-images.yml` pushes only on
  `release:published`/`workflow_dispatch`, so that image was built by every push to `main` and
  published by none — `-profile stare_ml` failed on image pull for anyone who had not manually
  dispatched its build. One image that always works beats two where the second does not exist.
  **If you scripted `-profile stare_ml`, drop it**: it is no longer a valid profile name and
  Nextflow will refuse the run.
- **Published-output change:** two new registration artifacts are now published under
  `<outdir>/<patient_id>/registered/`, for external benchmarks that need them:
  `registered/transform/preprocessed/data/<patient_id>_registrar.pickle` (the VALIS registrar,
  REGISTER's own `registrar` emit — previously produced but never written to the published
  tree) and `registered/controls/<patient_id>_<channels>_<ix>_<iy>_ctrl.json` (STARE's
  per-tile control-point JSON: the only on-disk record of the per-tile `error`/`ref_fg`/
  `mov_fg` values behind the tiled path's accept/reject gate). **Metadata-pressure note:** the
  control-JSON file COUNT scales as (slide/tile)², not linearly — `--reg_tiled_mode low` (a
  larger tile size) is not simply "fewer, bigger files" the way it is for other STARE
  intermediates; on a 20480² slide it is on the order of ~100 files at the shipped `high` tier
  vs. an estimated ~1600 at `low`. Each file is still only a few hundred bytes, so total bytes
  stay negligible (tens of KB per slide), but the file *count* is what pressures a networked
  filesystem's metadata server (Lustre/GPFS), not disk usage.
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
- **`pixel_size` default: `null` → `'auto'`.** It used to have no shipped default —
  `ParamUtils.validatePixelSize` forced an operator to assert a positive number of
  micrometres per pixel, or pass `'auto'` explicitly, before a run could start.
  `'auto'` is now the default: it reads `PhysicalSizeX` from each image's own OME
  header via the new `PREFLIGHT_SCALE` process (entry above). `conf/test.config` and
  `conf/test_full.config` still pin an explicit number, because the synthetic test
  fixtures carry no OME `PhysicalSize` and `'auto'` would (correctly) refuse them.
  Every checkpoint manifest (`lib/Checkpoint.groovy`'s per-step schema) now also
  carries a `pixel_size` column recording the resolved value; a checkpoint CSV
  written before this change has no such column and is REFUSED on re-entry
  (`--start`, `--prior_outdir`) with a "this checkpoint predates scale tracking"
  error rather than silently falling back to `params.pixel_size` — the remedy is to
  re-run the step that wrote the checkpoint.
- **`reg_tiled_dapi_index` → `reg_tiled_nuclear_index`, default `0` → `null`.** `null`
  resolves the index from the slide's own channel metadata via
  `MarkerUtils.nuclearIndex`, matching how `SEGMENT`'s CellSAM backend already resolves
  it (`lib/SegBackends.groovy`); an integer overrides. `TILED_COARSE` and
  `TILED_REG_TILE` fail loudly, naming the configured markers, when no nuclear channel
  is present. The Python flag is now `--nuclear-index`, with `--dapi-index` kept as a
  deprecated alias so hand-run commands still work. `tiled_io.dapi_channel` is renamed
  `nuclear_channel`. **Breaking for anyone passing `--reg_tiled_dapi_index` on the
  command line or in a params file** — rename it.
- `bin/tiled_coarse.py`'s range check was `index >= min(C_ref, C_mov)`, which a
  negative index passes before silently reading the *last* channel through Python's
  wraparound. Now `0 <= index < min(...)`.

- **The tiled (STARE) steps now reserve memory derived from the parameter that sets it,
  instead of a flat constant.** Every tiled process was designed so its peak is a function of
  a *parameter* rather than of the slide (`tiled_adapter.nf`'s header). Measured peak RSS on
  this codebase confirms the design holds — `TILED_COARSE` 1.97 GB, `TILED_REG_TILE` 1.33 GB,
  `TILED_SOLVE` 0.03 GB, `TILED_STITCH` 1.42 GB, all on an 8192² pair, and flat in slide size
  because each task reads a bounded window through lazy zarr regions. But the *reservations*
  were constants, so the guarantee was only a comment: raising `reg_tiled_tile` or
  `reg_tiled_out_tile` grew the need while the request stayed at 8 GB, and the overrun
  surfaced as a SIGKILL rather than a validation error. `conf/modules.config` now derives both
  from the measured cost line (`REG_TILE ≈ 0.4 GB + 0.10 GB/Mpx` of `(tile + 2·halo)²`;
  `STITCH ≈ 0.6 GB + 0.17 GB/Mpx` of `out_tile²`), doubled, with a 4 GB floor. Shipped defaults
  now ask **4 GB instead of 8 GB** for both, while `--reg_tiled_tile 4096` reserves 6 GB and
  `--reg_tiled_out_tile 4096` reserves 7 GB (verified against `-with-trace`). `TILED_SOLVE`
  drops **4 GB → 1 GB**: it was reserving ~120× its measured 33 MB peak for a 0.11 s reduction
  over a few hundred bytes of JSON per tile. `TILED_COARSE` is deliberately left on its
  doubling ramp — unlike the other three, part of its peak is the source plane's zarr chunk,
  which is slide-dependent and cannot be bounded from a parameter.
  - Both helpers are **self-contained by necessity**, not by preference. A top-level `def` in
    `conf/modules.config` can be called *from* a resource closure (as `starDistCommonFlags()`
    is from `ext.args`), but inside that invocation it cannot call *another* top-level `def` —
    the body resolves against the closure's delegate, not the config script. Factoring the
    shared arithmetic into one `tiledWindowGb(px, base, slope)` helper parsed fine and then
    aborted every task with `Unknown method invocation 'tiledWindowGb' on _parse_closure5
    type`. This is the same class of trap as "conf/*.config cannot see lib/*.groovy", and it
    is equally invisible until a real submission: **`-stub` does not exercise these closures.**
- **The tiled size parameters are validated up front.** `reg_tiled_tile`, `reg_tiled_halo`,
  `reg_tiled_out_tile`, `reg_tiled_upsample`, `reg_tiled_dapi_index` and `reg_tiled_gate_tre`
  had no `minimum` and no `ParamUtils` check, so nonsense values failed deep inside a container
  or not at all. `reg_tiled_out_tile` additionally now carries `multipleOf: 16`: tifffile
  rejects any other tile shape, so `--reg_tiled_out_tile 1000` used to run `TILED_COARSE`,
  every `TILED_REG_TILE` and `TILED_SOLVE` before dying in the final `TILED_STITCH` with
  `ValueError: invalid tile shape`. It is now caught by `validateParameters()` before any
  process is instantiated.
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
  heredocs. `modules/local/segment.nf` is no longer the exception it started as: rather
  than holding each backend's version rows PRE-RENDERED, `lib/SegBackends.groovy` now
  stores them as bare module names (`versionTools`, the same key `lib/WarpBackends.groovy`
  uses) and `segment.nf`'s two blocks render from that one list like every other module's.
  That leaves 23 of the 24 `modules/local/*.nf` files routed through `ProcessEnvelope`,
  with `aggregate_size_logs.nf` the sole documented exception — it runs in
  `ubuntu:22.04`, has no Python interpreter, and reports a `bash:` row that
  `ProcessEnvelope`'s automatic `python:` row would falsify. No published `versions.yml` content
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
- **Three `DAPI` hardcodes removed; the nuclear/fiducial channel is now resolved the
  same way everywhere.** `params.nuclear_markers` was already the declared source of
  truth (`lib/MarkerUtils.groovy`, `bin/utils/metadata.py`), but three sites still
  spelled `DAPI` themselves:
  - `bin/utils/qc.py`'s `create_registration_qc` picked the overlay channel with
    `"DAPI" in ch.upper()`. `GENERATE_REGISTRATION_QC` was never handed
    `nuclear_markers` at all, so the resolver could not be used even in principle. It
    now receives `--nuclear-markers` (same `MarkerUtils.markerList` normalisation
    `SPLIT_CHANNELS` uses) and resolves via `metadata.pick_nuclear_index`. The old
    code was correct only by coincidence — it fell back to channel 0, which
    `CONVERT_IMAGE` reserves for the nuclear channel — so the visible symptom was a
    `No DAPI channel found ... Falling back to channel 0` warning on every slide of a
    CellTox panel, and the latent one was picking the wrong channel had that promotion
    invariant ever changed. `create_dapi_overlay` is renamed `create_nuclear_overlay`.
  - `bin/register.py`'s `--reference-markers` defaulted to `["DAPI", "SMA"]`, a
    cohort-specific pair matched against the **filename** — which the rest of the
    pipeline forbids (channel identity comes from metadata). The default is now `None`;
    with neither `--reference` nor `--reference-markers`, the alphabetically first
    slide is used. Unreachable from the pipeline either way: `modules/local/register.nf`
    always passes `--reference`.
  - STARE's channel index was the fixed param `reg_tiled_dapi_index = 0`, restating an
    invariant `CONVERT_IMAGE` already guarantees under one marker's name.

- **A scale disagreement between an image and `params.pixel_size` was silent.**
  `params.pixel_size` is the single owner of every µm conversion in the pipeline — the
  measurements in `cells.geojson`, the published pyramid's `PhysicalSize`, InstantSeg's
  rescaling — but nothing ever compared it against what the input images themselves
  claimed. A cohort scanned at a different objective was converted, registered,
  segmented and quantified at the configured scale with nothing said, and the symptom
  surfaced in QuPath: the sibling `qupath-extension-flowpath` warns about a scale it
  can see but cannot explain. `docs/parameters.md` additionally documented the opposite
  of the truth ("the real value is read from the input OME metadata and **preserved**
  through the pipeline"), which is corrected here.
  - The rule now lives in one place, `bin/utils/pixel_size.py`. `CONVERT_IMAGE` and
    `PREPROCESS` logged `[SCALE MISMATCH]` naming both numbers and the percentage apart
    when an input's OME `PhysicalSize` differs from the configured value by more than
    1%. Ownership is unchanged by design: `warn_on_pixel_size_mismatch` returns a
    bool, never a scale, so no caller can start preferring the metadata and become a
    second owner. **No published number changes.** (`PREPROCESS` was later deleted and
    its illumination-correction role replaced by nf-core's `BASICPY`, entry below;
    `CONVERT_IMAGE` still logs this warning.)
  - `bin/preprocess.py` had its own OME-XML walk with its own hardcoded `0.325`
    fallback, so a run with `--pixel_size` set to anything else silently fell back to
    a number nothing in the run had asked for. It was switched to the shared reader,
    falling back to the configured value, before the file itself was deleted entirely
    (entry below).
  - `bin/convert_image.py`'s readers returned `PIXEL_SIZE_UM` where a file said
    nothing, making "detected" and "absent" indistinguishable. They return `None`; the
    fallback is applied once, at the point the two values are compared.
  - **`SPLIT_CHANNELS` and `TILED_STITCH` wrote their outputs with no scale at all** —
    a bare `imwrite`, no resolution tags, no OME header — so every file between
    registration and the pyramid claimed 1 px = 1 unit and `MERGE_AND_PYRAMID` had
    nothing to read even in principle. Both now stamp the configured scale as standard
    TIFF resolution tags (not an OME header: that would be a second, channel-less
    source of channel names for `SPLIT_CHANNELS` to find).
  - Guarded in both directions. `tests/test_pixel_size_is_passed.py` fails if a `bin/`
    script that accepts a scale is not handed one (it would silently use its argparse
    default — a second owner), resolving `SEGMENT`'s backend through
    `lib/SegBackends.groovy` rather than by name. `tests/modules/split_channels.nf.test`
    asserts the *rendered* `--pixel-size 0.325`, tagged `stub` but deliberately without
    `options "-stub"`, since `-stub` never evaluates a `script:` block.
- **`cells.geojson` now carries the segmentation `label` as a measurement.** It was the
  one identity column `cells_data.csv` kept and the GeoJSON dropped: `label` is in
  `MORPHOLOGY_COLS`, which `merge_quant_csvs.reorder_columns` uses to *group* columns
  (they all stay in the CSV) but which `export_geojson` uses to *exclude* from the
  measurement array. The two published artefacts were therefore joinable only by
  position — and position is wrong whenever a cell has a NaN centroid, since that row
  stays in the CSV and never becomes a feature (`export_geojson()`'s `skipped`
  counter), silently and undetectably from the artefacts alone. `bin/join_flowpath.py`
  already warned about exactly this ("Add the label measurement to the GeoJSON export
  to make this exact") and fell back to a centroid join, whose correctness depends on
  `--pixel-size` being right; the exact label join is now the reachable path. Costs
  nothing downstream: FlowPath's `DetectionIngest.MORPHOLOGY_PREFIXES` already filters
  `label` out of the marker panel, and `CellIndex` already resolves a measurement named
  `label`.
- **A `nextflow.extension.GroupKey` leaked out of `groupTuple()` and travelled as a
  channel key, silently dropping registration QC on roughly half of contended runs.**
  `subworkflows/local/registration.nf` passed the object `groupTuple()` emits straight on
  as the patient id, into `REGISTER`'s `val(patient_id)` and back out on its outputs.
  `GroupKey` delegates `hashCode()` to the String it wraps but its `equals()` is
  **asymmetric** (measured on Nextflow 25.04.7): `groupKey('P001', 2).equals('P001')` is
  true, `'P001'.equals(groupKey('P001', 2))` is false, and both hash to 2430945. Equal
  hashes put the two in the same `HashMap` bucket and `HashMap` compares
  `queryKey.equals(storedKey)`, so the lookup succeeds in exactly one direction —
  and `CombineOp.emit()` stores each side of a `combine(by:)` in such a map, so whichever
  side arrives FIRST decides the stored key type. In `subworkflows/local/seg_qc.nf`'s
  VALIS warp fan-in: GeoJSON first, the key is stored as a String and the later `GroupKey`
  lookup hits; transform first, the key is stored as a `GroupKey` and the later String
  lookup **misses**, `WARP_SEG_QC` is started but never receives an input tuple, and the
  patient's whole registration QC (`*_seg_qc.json`) is silently absent from a run that
  exits 0. Idle, `conf/test.config`'s `max_cpus = 2` fixes the arrival order and the safe
  side always won (12/12 green), which is how this survived; under CPU contention it
  dropped 14 of 24 runs, every one exiting 0. Fixed at the source — the groupKey is a
  streaming size hint and has no business leaving its own `groupTuple()` — so `seg_qc.nf`,
  `conf/modules.config` and `warp_seg_qc.nf` are untouched: no `errorStrategy` change, no
  retry, the metrics output stays mandatory. After the fix the contended configuration is
  24/24 and the sequential one 12/12. `tests/test_group_key_unwrapped.py` fails if any
  `groupKey()` ever escapes its `groupTuple()` again. **`-resume` is unaffected and
  existing work directories stay valid:** `GroupKey` implements `CacheFunnel` and funnels
  its wrapped target, so task hashes were never sensitive to the wrapper.
- **`REGISTER` wrote its staging file list to a fixed `/tmp/files_to_copy.txt`.** The
  process is per-patient with concurrent forks, and under Singularity `/tmp` is
  host-bind-mounted by default, so two concurrent `REGISTER` tasks on one node could share
  that one file and each stage the other's slide list. (Never observed under Docker, where
  each task gets its own `/tmp`.) All three references now use a plain task-relative
  filename, so the list lives inside the task's own work directory; `cp -Ln`,
  `xargs -P ${task.cpus}` and the `find -L` expression are unchanged. Covered by a
  rendered-command nf-test rather than a stub one, since `-stub` never evaluates a
  `script:` block and cannot see this class of fix.
- **The `reg_qc=2` registration QC now segments with the run's own segmenter, and at
  stock defaults it runs at all.** `SEG_QC_GEOJSON` ran StarDist itself
  (`bin/segment_to_geojson.py` + `starDistCommonFlags()`), making it the one segmenter
  in the pipeline that ignored `params.seg_method`. Two consequences, both live:
  at the shipped default (`seg_method='instantseg'`) the QC scored cells found by a
  *different* segmenter than the one whose masks the run ships; and because
  `segmentation_model_dir` defaults to null while `segmentation_model` is not a StarDist
  built-in, the process raised `FileNotFoundError` — swallowed by the QC block's
  `errorStrategy` `'ignore'` branch, so a stock run (where `reg_qc = 2` is the DEFAULT)
  silently produced **no registration QC at all** and still exited 0. Only
  `conf/test.config` escaped it, by overriding `segmentation_model` to a real built-in.
  `subworkflows/local/seg_qc.nf` now segments the native slides with `SEGMENT` itself,
  aliased `SEG_QC_SEGMENT`, so the backend follows `--seg_method` by construction;
  `SEG_QC_GEOJSON` keeps only the label-image → polygon half, via `bin/mask_to_geojson.py`
  (now tracked executable — it is invoked by name for the first time).
  `bin/segment_to_geojson.py` is deleted.
  **Not a scoring change for a fixed backend.** The deleted script's last act was to call
  the same `mask_to_feature_collection()` on the same in-memory `cell_mask`; the only new
  step is a lossless zlib-TIFF round trip, pinned by
  `tests/test_seg_qc_geojson_equivalence.py`. **Two things do change on purpose:** the
  default run now segments with InstanSeg rather than StarDist (that is the fix), and on
  the stardist backend the nuclear channel is chosen positionally (`--dapi-channel 0`,
  guarded by `SegBackends`' channel-0 check) instead of resolved from OME channel names.
  These agree for `CONVERT_IMAGE` output, which is everything reaching this QC, because
  `CONVERT_IMAGE` normalises the nuclear channel to index 0. Where they diverge, they
  diverge in two directions and both are deliberate: a slide whose nuclear marker sits at
  index *n* > 0 was previously segmented at *n* and now fails the guard instead — the same
  contract `SEGMENT` already enforces on the registered image, so the QC stops being more
  permissive than the pipeline it is measuring; and a slide with no matching marker at all
  was previously segmented at index 0 *silently* (`find_nuclear_index`'s fallback) and now
  fails there too. A failure here is swallowed by the QC `errorStrategy`, so the visible
  effect in both cases is a missing GeoJSON rather than a failed run.
  **Published output is unchanged:** verified by diffing a full `-profile test -stub`
  tree against the same run at `1ad1271` — 62 files both sides, identical but for the
  QC report's timestamped filename. The QC's per-slide masks are deliberately not
  published (`publishDir = [ enabled: false ]`): `<pid>/segmentation/` is indexed by
  `csv/segmented.csv`, which names exactly one per-patient mask.
- **An aliased process silently inherited the base process' `publishDir`.** Nextflow
  matches a config selector against an alias' own name *and* its process' original
  declared name, so `withName: 'SEGMENT'` governs `SEG_QC_SEGMENT` too. That is wanted for
  container/resources/`ext.args` and a data-loss bug for `publishDir` — it already fired
  once here, on `SPLIT_CHANNELS`/`SPLIT_PRIOR_PYRAMID`. `tests/test_process_alias_config.py`
  now fails if any alias of a publishing process lacks its own `publishDir` override, or
  places it textually *before* the block it means to override (later matching selectors
  win, per field).
- **`SEGMENT`'s size log is named from `task.ext.prefix`, not `meta.patient_id`.**
  Unchanged for `SEGMENT` itself (one task per patient), but the QC alias runs once per
  *slide*, so every slide of a patient would have written the same
  `<pid>.SEGMENT.size.csv` into the directory `AGGREGATE_SIZE_LOGS` stages them all into.

- **`TILED_COARSE` never finished inside its 2 h walltime — ORB was being thresholded
  against raw uint16 counts.** scikit-image's `ORB(fast_threshold=...)` is an *absolute*
  intensity difference, and `img_as_float` rescales only **integer** inputs — so the
  `np.asarray(img, dtype=float)` cast in `coarse_align._orb_features` was precisely what
  stopped the data being rescaled. The planes reaching it come from
  `tiled_io.read_decimated` as raw microscopy counts widened to float32 (0..65535), where
  the `0.05` default sits ~6 orders of magnitude below the data range: FAST fired on ~38%
  of all pixels instead of ~3%, and `corner_peaks`' spacing pass — quadratic in the
  candidate count — turned the anchor into a multi-hour step. Measured on a 2048² thumbnail,
  same image: **8.0 s normalised vs 522.1 s raw (65×)**; the gap widens with area (~×15.7 per
  ×4 px), so at the `reg_tiled_coarse_max_dim=4096` default it is ~2.8 h *per slide* — two
  slides per task, against `process_low`'s `2.h * task.attempt`. Nothing failed loudly: the
  step has no intermediate output, so it presented as a hang and then a walltime kill.
  `coarse_align.normalize_for_orb` now rescales each plane into [0, 1] over a robust
  percentile window (p1–p99.9, not `img/img.max()`, which one hot pixel would wreck) before
  ORB sees it. This is a **correctness** fix as much as a speed one: reference and moving
  come from different cycles with different exposures, so a single absolute threshold applied
  to two different dynamic ranges could starve one slide of keypoints and silently produce a
  bogus `M0`. `reg_benchmark._orb_xy` carried the identical bug and now imports the same
  helper — that path is the more exposed of the two, since `feature_residual` scores
  full-resolution registered slides rather than a bounded thumbnail. The existing ORB smoke
  test could never have caught this: it normalises its own input, so it only ever exercised
  the healthy regime. `tests/test_coarse_align.py` now pins scale-invariance directly
  (same field at 1× and 65535× must yield identical keypoints) rather than asserting a
  wall-clock time, which would be flaky on shared CI and would test the symptom.
- **`TILED_COARSE` was silent for its entire runtime.** It emitted exactly one log line, at
  the very end, so a stuck or merely slow anchor was indistinguishable from a hang and left
  nothing in `.command.out` to diagnose. It now logs each stage as it completes — inputs and
  sizes, both slides' dimensions/dtype/channel counts, the decimation factor and band size,
  per-slide read time with the observed **intensity percentiles** (the summary that makes the
  scale bug above visible rather than fatal), per-slide ORB keypoint counts and timings, the
  match count, the resulting `M0` translation/rotation, and the tile count as the downstream
  fan-out width. It also now warns on conditions that previously exited 0 in silence: a
  residual at or beyond `--halo` (the per-tile step reads a window that will not contain the
  true match — a mis-registered slide that still succeeds), fewer than 10 inliers, too few
  matches for the requested model, a non-finite residual, and a `--dapi-index` out of range
  for either slide.
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
- **Automated panel-agnostic phenotyping is extracted off this branch, and preserved
  intact on `feat/automated-phenotyping` (at `56a3a46`).** It is work in progress and
  is parked there rather than deleted. Gone from here: `COMPILE_PANEL` and `PHENOTYPE`
  (`bin/compile_panel.py`, `bin/phenotype_cells.py`, `bin/utils/phenotyping/`), the five
  parameters `panel_spec` / `panel_model` / `pheno_alpha` / `pheno_min_cal` /
  `pheno_max_enumerate` (the params block now declares 75, was 80), the `phenotyping/`
  publish kind, `assets/import_phenotype.groovy`, `panel.yaml`, and 22 test files
  (20 pytest + `tests/modules/{compile_panel,phenotype}.nf.test`). `EXPORT_GEOJSON` is
  back to a four-slot input tuple and emits the constant `"Cell"` classification with no
  `panel_model.json` sidecar — gate downstream in QuPath/FlowPath. The one case in
  `test_export_geojson_phenotype.py` that was NOT about phenotyping — the constant
  `"Cell"`, no-stamped-`id` contract — survives as `tests/test_export_geojson.py`.
  The FlowPath-side phenotype join (`bin/join_flowpath.py`,
  `export_spatialdata.py --attach-phenotypes`) is unaffected; it is a different,
  post-pipeline feature.
- **`reg_tiled_fanout` is gone, along with the single-task `TILED_REGISTER` path it
  selected.** STARE now has exactly one execution shape: the per-tile fan-out
  `TILED_COARSE` → `TILED_REG_TILE` (×tiles) → `TILED_SOLVE` → `TILED_STITCH`. Its memory is
  bounded — every process' peak is a function of a parameter (`reg_tiled_coarse_max_dim`,
  `reg_tiled_tile` + `reg_tiled_halo`, `reg_tiled_out_tile`) rather than of the slide's
  dimensions. Measured peak RSS on a 16384² 2-channel tiled OME-TIFF: **0.91 / 1.31 / <1.31 /
  1.35 GB** against an 8 GB budget.

  The removed alternative held both whole slides, a float32 all-channel copy (`mov_hwc`) and the
  full-size warped output simultaneously, and `tiled_pipeline.register_slide` additionally
  materialised a whole-slide float64 `mov_rigid` — roughly 10–15× the decompressed moving stack,
  which is why its budget had to be derived from file size. It produced identical pixels and an
  identical manifest, so nothing is lost but the footgun: a flag whose "off" position silently
  reintroduced unbounded memory.

  **Deleted:** `modules/local/tiled_register.nf`, `bin/tiled_register.py`,
  `tests/test_tiled_register_cli.py`, the `withName: 'TILED_REGISTER'` block in
  `conf/modules.config`, the `params.reg_tiled_fanout` declaration and its
  `nextflow_schema.json` property, and the branch in
  `subworkflows/local/adapters/tiled_adapter.nf` (the adapter now runs the fan-out
  unconditionally). `bin/utils/tiled_pipeline.py` is **kept** — still imported by
  `tests/test_tiled_pipeline.py` and `tests/test_reg_benchmark.py`, though it no longer has a
  production caller.

  Scheduler load is the trade: at the default `reg_tiled_tile = 2048` a 26k² slide plans ~169
  `TILED_REG_TILE` tasks per moving slide (capped by `maxForks = 20`). Raise `reg_tiled_tile` to
  trade task count against mesh resolution.

- **`reg_reference_markers`** — removed. It only fed a filename-based fallback for reference-image
  selection that the pipeline never reached (the reference slide is chosen by `is_reference`, and
  `--reference` is always passed to VALIS). Nuclear-channel selection is now `nuclear_markers`
  (metadata-based). `register.py`'s standalone `--reference-markers` CLI flag is unaffected.
- **`padding` / `pad_mode`** and the `PAD_IMAGES`, `GET_IMAGE_DIMS`, and `MAX_DIM` processes —
  removed. Padding was default-off and unused: both registration backends align inputs of differing
  sizes natively (VALIS into a shared space; the tiled/STARE backend into the reference's shape).
  This also removed the pipeline's only filename-based channel-name inference.
- **`PREPROCESS` and `bin/preprocess.py` — deleted.** In-process BaSiCPy illumination
  correction (one process, one Python script, its own thread pool over per-channel BaSiC
  fits) is replaced by three processes wrapping nf-core's vendored `BASICPY` module:
  `TILE_FOR_BASIC` (writes the slide onto the pseudo-FOV grid the module requires — it
  computes profiles only, refuses a single-sited image, and expects mcmicro/ASHLAR to
  apply them, which mirage does not have) → `BASICPY` (fits flatfield/darkfield profiles)
  → `APPLY_PROFILES` (the arithmetic the module leaves out, plus reassembly). See
  `subworkflows/local/preprocess.nf` and `modules/nf-core/basicpy/MIRAGE-NOTES.md`.
  - **Illumination-correction MODEL change, published-output change.** `BASICPY` runs at
    its upstream defaults, which means `get_darkfield=False`: **no additive darkfield is
    estimated or removed any more.** Mirage's deleted in-process path called
    `BaSiC(get_darkfield=True, smoothness_flatfield=1)` explicitly. Every corrected pixel
    downstream of preprocessing — registration, segmentation, quantification, exported
    intensities — is therefore computed from a flatfield-only correction, not the
    flatfield+darkfield correction the pipeline shipped until now. `bin/apply_basic_profiles.py`
    still accepts an optional `--darkfield` and subtracts it when supplied (a fitted
    darkfield remains all zeros at these defaults, so the subtraction is a no-op in
    production); there is no pipeline knob that supplies one.
  - **New third-party container the repo does not build.**
    `docker.io/labsyspharm/basicpy-docker-mcmicro:1.2.0-patch5`
    (`modules/nf-core/basicpy/main.nf:5`) is vendored unmodified from mcmicro, unlike
    every other process image, which is `bolt3x/mirage-<component>` and built by
    `containers/<component>/Dockerfile` + `.github/workflows/build-images.yml`. It is now
    a listed exception in `tests/test_container_image_naming.py`'s `EXTERNAL_ALLOWED`,
    whose scan was widened to cover `modules/nf-core/` (previously `modules/local/` +
    `lib/` + `conf/` only, so this reference was never checked at all).
- **`bin/convert_image.py`'s BioIO read is now lazy.** `img.data` (an eager, whole-array
  materialisation) is replaced by `img.dask_data`, and the OME-TIFF write iterates its
  chunks instead of holding the decoded array whole. Byte-identical output
  (`tests/test_convert_lazy_read.py`, `tests/test_convert_streaming_write.py`); see
  `docs/figures/zarr-schematic.html` panel **d** for the measured peak-RSS shape
  (435.2 MB eager → 167.7 MB streamed per-plane, on a 144 MB fixture; ratio not claimed to
  hold at production scale). `TILE_FOR_BASIC` and `APPLY_PROFILES` read through the same
  lazy primitive by construction (`docs/figures/zarr-schematic.html` panel **c**, "the
  fifth site").

## [1.0.0] - 2026-07-29

First public release. End-to-end multiplex WSI processing: preprocessing, registration,
segmentation, per-cell quantification, and QuPath-compatible GeoJSON + pyramidal
OME-TIFF export.

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
