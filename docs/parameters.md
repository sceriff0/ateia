# Parameters

The complete, canonical parameter surface, grouped by pipeline stage. Defaults
shown here are the values in
[`nextflow.config`](https://github.com/sceriff0/mirage/blob/main/nextflow.config).

!!! abstract "Canonical sources"
    - **Defaults** — `nextflow.config` (`params { … }`)
    - **Validation & enums** — `lib/ParamUtils.groovy`
    - **Schema** — `nextflow_schema.json`

    Pass values on the command line (`--param value`), or load a preset with
    `-params-file params/full_pipeline.json` and override individual values. Note
    the convention: pipeline parameters use a **double dash** (`--input`);
    Nextflow's own options use a **single dash** (`-profile`, `-resume`,
    `-params-file`).

## Input / output

| Parameter | Default | Description |
|---|---|---|
| `input` | `null` | Path to the samplesheet CSV. **Required.** Columns depend on `--start` — see [Samplesheet & Input](usage.md#the-samplesheet). |
| `outdir` | `null` | Output root directory. **Required** — no default. Checkpoint CSVs are written to `<outdir>/csv/`. |

## Workflow control

| Parameter | Default | Description |
|---|---|---|
| `start` | `preprocessing` | Entry stage: `preprocessing`, `registration`, `segmentation`, or `postprocessing`. |
| `stop` | `null` | Stop after this stage. `null` = run to the end. |
| `dry_run` | `false` | Validate inputs and exit without launching tasks. |
| `debug_channels` | `false` | Emit `.view` channel-topology debug output. |

!!! tip "Run a single stage"
    `--start registration --stop registration` runs registration only, reading a
    `<outdir>/csv/preprocessed.csv` checkpoint. See [Restartability](usage.md#checkpoints-resuming).

## Preprocessing

Bio-Formats conversion + BaSiC illumination correction.

| Parameter | Default | Description |
|---|---|---|
| `pixel_size` | `0.325` | Fallback physical pixel size in µm. The real value is read from the input OME metadata and **preserved** through the pipeline; `0.325` is used only when the input carries no pixel size. Governs µm conversion in GeoJSON and InstanSeg. |
| `preproc_tile_size` | `1950` | FOV tile size (px) for BaSiC. |
| `preproc_skip_dapi` | `true` | Skip BaSiC correction on the DAPI channel. |
| `preproc_autotune` | `false` | Enable BaSiC autotune. |
| `preproc_n_iter` | `100` | BaSiC optimization iterations. |
| `preproc_pool_workers` | `null` | Worker threads for BaSiC preprocessing. `null` = use the process CPU count (`task.cpus`). |
| `preproc_overlap` | `0` | FOV tile overlap (px). |
| `preproc_no_darkfield` | `false` | Disable darkfield estimation. |

## Registration

Two backends, selected by `--registration_method`: **VALIS** (default, graph-based
whole-slide alignment) and **STARE tiled** (JVM-free, fully parallel, laptop-friendly).

### Common

| Parameter | Default | Description |
|---|---|---|
| `registration_method` | `valis` | Registration backend: `valis` (VALIS whole-slide) or `tiled` (STARE — see [Tiled / STARE](#tiled--stare-registration_methodtiled)). |
| `allow_auto_reference` | `false` | If no `is_reference=true` row, use the first panel as reference. |
| `nuclear_markers` | `['DAPI','CELLTOX']` | Ordered preference of nuclear/fiducial marker names. The first present (resolved from channel metadata, never the filename) is moved to channel 0 and drives both cell segmentation and the registration fiducial. Fails fast if none present (single-channel images excepted). Accepts a **list** (config or `-params-file`) or a **comma/space-separated string**, which is the only shape a command line can produce: `--nuclear_markers CELLTOX` and `--nuclear_markers DAPI,CELLTOX` both work. Matching is case-insensitive **substring**, so `DAPI_nuclear` counts as nuclear. |

### VALIS

| Parameter | Default | Description |
|---|---|---|
| `memory_mode` | `high` | `low` (BRISK/RANSAC, small dims), `medium`, or `high` (SuperPoint/SuperGlue, larger dims). |
| `reg_micro_reg_fraction` | `0.125` | Image fraction used for micro-registration. |
| `reg_max_image_dim` | `4000` | Max cached image dimension during registration. |
| `reg_micro_reg` | `2` | Micro-registration depth (nested, default MAX): `0` = none, `1` = micro-rigid only (refines `slide.M`), `2` = + micro non-rigid (`register_micro`). At `>=1` the QC `rigid` stage means affine ∘ micro-rigid. |
| `reg_jvm_heap_gb` | `null` | Explicit JVM heap (GB) for VALIS. `null` auto-estimates from input size. |
| `reg_qc` | `2` | Registration QC depth: `0` = none, `1` = DAPI overlay only, `2` = DAPI overlay + [staged segmentation-overlap metrics](registration_qc.md). |

At `reg_qc = 2` the pipeline segments each slide's DAPI on its **native** image, pairs the
nuclei once after rigid registration, and then re-scores those same pairs after every later
stage — so per-pair IoU and centroid residual can be attributed to `rigid`, `non_rigid` and
`micro` individually. See [Staged registration QC](registration_qc.md) for the output schema
and how to read it.

### Tiled / STARE (`registration_method=tiled`)

STARE is an alternative registration backend that is **JVM-free** (no Bio-Formats/Java
heap), tiles the slide internally, and runs fully in parallel — usable on a laptop. These
params apply only when `--registration_method tiled`.

| Parameter | Default | Description |
|---|---|---|
| `reg_tiled_dapi_index` | `0` | DAPI channel index used to estimate the transform. |
| `reg_tiled_tile` | `2048` | Tile core size (px); also the mesh-grid resolution. |
| `reg_tiled_halo` | `256` | Per-tile read halo (px) for registration context. |
| `reg_tiled_gate_tre` | `1.0` | Refine only tiles whose rigid-stage TRE (px) exceeds this. |
| `reg_tiled_upsample` | `10` | Phase-correlation sub-pixel upsample factor (per tile). |
| `reg_tiled_out_tile` | `1024` | Streaming stitch write-tile size (px) — gigapixel-safe. |
| `reg_tiled_coarse_max_dim` | `4096` | Longest side (px) of the thumbnail the coarse anchor (M0) is estimated on. **This is what bounds COARSE memory**, not tile size: ORB costs ~40 bytes per source pixel, so a native-resolution anchor on a gigapixel slide needs tens of GB. Lower = cheaper and coarser; the M0 residual grows with the decimation factor and must stay well inside `reg_tiled_halo`. `0` disables decimation. |

## Segmentation

Backend selected by `--seg_method`.

### Backend selection & shared

| Parameter | Default | Description |
|---|---|---|
| `seg_method` | `instantseg` | Backend: `stardist`, `instantseg`, or `cellsam`. The container is chosen automatically. `instantseg` is the default because it is the only backend that runs on a fresh clone — `stardist` needs a trained model supplied via `segmentation_model_dir`, and `cellsam` needs a DeepCell access token. InstanSeg is Apache-2.0 with unencumbered weights. |
| `seg_gpu` | `true` | Use GPU (adds SLURM `--gres` + Singularity `--nv`). Set `false` to force CPU. |
| `seg_expand_distance` | `10` | Pixels to expand nuclei into the whole-cell mask (StarDist & CellSAM). |

### StarDist (`seg_method=stardist`)

Consumes the **DAPI channel** (must be channel 0 — guaranteed upstream).

| Parameter | Default | Description |
|---|---|---|
| `segmentation_model` | `stardist_full_e200_…` | StarDist model name. **Not a StarDist built-in** — it names a trained model that must exist under `segmentation_model_dir`. |
| `segmentation_model_dir` | `null` | Directory holding the trained model. **Required for this backend**: with `null`, `segment.py` raises `FileNotFoundError` unless `segmentation_model` names a StarDist built-in (`2D_versatile_fluo`, `2D_versatile_he`, `2D_paper_dsb2018`, `2D_demo`). No model is bundled with the repo. |
| `seg_pmin` | `1.0` | Lower percentile for normalization. |
| `seg_pmax` | `99.8` | Upper percentile for normalization. |
| `seg_n_tiles_x` | `16` | Inference tiles along X. |
| `seg_n_tiles_y` | `16` | Inference tiles along Y. |
| `seg_prob_thresh` | `null` | Detection probability threshold. `null` uses the model's built-in default. |

### InstanSeg (`seg_method=instantseg`)

Channel-invariant — consumes the full multichannel image.

| Parameter | Default | Description |
|---|---|---|
| `seg_instantseg_model` | `fluorescence_nuclei_and_cells` | Pretrained InstanSeg model. |
| `seg_instantseg_target` | `all_outputs` | `all_outputs`, `cells`, or `nuclei`. |
| `seg_instantseg_tile_size` | `512` | Sliding-window tile size (px). |
| `seg_instantseg_batch_size` | `16` | Tiles per forward pass. Raise on big GPUs; lower to avoid OOM. |
| `instanseg_model_dir` | `null` | Writable BioImage.IO model cache (exported as `INSTANSEG_BIOIMAGEIO_PATH`). |

### CellSAM (`seg_method=cellsam`)

SAM foundation model on the DAPI channel (located by name).

| Parameter | Default | Description |
|---|---|---|
| `seg_cellsam_bbox_threshold` | `0.4` | Bounding-box confidence — the main precision/recall knob. |
| `seg_cellsam_use_wsi` | `true` | Enable CellSAM native whole-slide tiling. |
| `seg_cellsam_block_size` | `1024` | Tile side (px) in WSI mode (recommend 256–2048). |
| `seg_cellsam_overlap` | `56` | Tile overlap (px) in WSI mode. |
| `cellsam_model_path` | `null` | Pre-downloaded weights. If `null`, weights auto-download and require `DEEPCELL_ACCESS_TOKEN`. |

!!! warning "CellSAM weights"
    With `cellsam_model_path = null`, export `DEEPCELL_ACCESS_TOKEN` before
    launching — the pipeline forwards it into the container. On clusters without
    compute-node internet, pre-download the weights and set `cellsam_model_path`.

## Quantification

Per-cell marker intensity.

| Parameter | Default | Description |
|---|---|---|
| `quantify_compartments` | `true` | Emit per-compartment signal (Nucleus / Cytoplasm / Cell) by routing the nuclear mask into quantification. |
| `expanded_quantification` | `true` | Also emit Mean and Sum per compartment (per-compartment Median is always emitted). **Requires** `quantify_compartments=true`. |

!!! danger "Validation rule"
    Setting `--expanded_quantification true` without `--quantify_compartments true`
    fails at launch with a clear error.

## Phenotyping (panel-agnostic)

Optional constrained cell-type classification. When **both** `panel_spec` and
`panel_model` are `null` (the default), phenotyping is skipped and the GeoJSON carries
the constant `"Cell"` classification (raw intensities only — gate downstream). Supplying a
panel enables conformal-risk-controlled (CRC) phenotype assignment written into the
exported `cells.geojson`.

| Parameter | Default | Description |
|---|---|---|
| `panel_spec` | `null` | Path to the panel authoring spec (`panel.yaml`). |
| `panel_model` | `null` | Path to a frozen, compiled `model_config.json` (produced by `COMPILE_PANEL`). |
| `pheno_alpha` | `0.05` | CRC target risk α (per-marker miscoverage budget). |
| `pheno_min_cal` | `50` | Minimum per-marker calibration-set size. |
| `pheno_max_enumerate` | `100000` | Feasible-set brute-force enumeration ceiling. |

!!! note "Incremental cycles"
    Phenotyping is **not** run in `--mode add_cycle`; setting `panel_spec`/`panel_model`
    in add_cycle mode is rejected at launch.

## Visualization & export

Pyramidal OME-TIFF assembly and GeoJSON.

| Parameter | Default | Description |
|---|---|---|
| `tilex` | `512` | Tile size (px, square) for the pyramidal OME-TIFF. |
| `pyramid_resolutions` | `8` | Number of pyramid resolution levels. |
| `pyramid_scale` | `2` | Downsampling factor between levels. |
| `compression` | `zstd` | Codec: `zstd`, `lzw`, `zlib`, `jpeg`, `none`. |
| `simplify_tolerance` | `1.0` | Douglas–Peucker tolerance (px) for GeoJSON contour simplification. Display-only — quantification is mask-derived, so this does not affect measurements. |
| `geojson_coord_precision` | `2` | Decimal places for GeoJSON coordinates. Display-only. |

## Quality control & reports { #quality-control--reports }

| Parameter | Default | Description |
|---|---|---|
| `skip_preprocess_qc` | `false` | Skip preprocessing QC thumbnails. |
| `preprocess_qc_scale_factor` | `0.25` | Downsample factor for preprocessing QC images. |
| `skip_registration_qc` | `false` | Skip registration QC overlays. |
| `qc_scale_factor` | `0.25` | Downsample factor for registration QC images. |
| `skip_postprocessing_qc` | `false` | Skip segmentation/intensity QC plots. |
| `skip_final_qc_report` | `false` | Skip the aggregated HTML QC report. |

### Reports

- **`qc/mirage_qc_report_<timestamp>.html`** — aggregated QC report: run summary,
  pipeline-stage status, sample manifest, preprocessing / registration (overlays,
  VALIS rTRE, STARE tiled TRE, warp-seg QC) / segmentation overlays /
  postprocessing QC, and software versions.
  Controlled by `skip_final_qc_report`.
- **`qc/mirage_resource_report.html`** — computational-resource report built from
  the per-task size logs and Nextflow `trace.txt`: run totals, per-process
  rollup, resource-vs-input-size, top-N heaviest/slowest tasks, and
  retries/failures. Generated at run completion when `enable_trace` is set;
  re-runnable by hand via `bin/generate_resource_report.py`. Complements
  Nextflow's native `report.html` / `timeline.html`.

## Cluster & resources

HPC details: [Running on HPC / SLURM](usage.md#running-on-hpc).

| Parameter | Default | Description |
|---|---|---|
| `slurm_partition` | `null` | SLURM partition (becomes the queue). |
| `slurm_account` | `null` | SLURM account (`--account`). |
| `slurm_qos` | `null` | SLURM QoS (`--qos`). |
| `gpu_type` | `nvidia_h200:1` | GRES string for GPU jobs (`--gres=gpu:<value>`). Match `sinfo -o "%G"`. |
| `max_memory` | `700.GB` | Global memory cap per process. |
| `max_cpus` | `128` | Global CPU cap per process. |
| `max_time` | `240.h` | Global walltime cap per process. |

!!! info "How resources scale"
    Per-process memory and time scale with `task.attempt`, so a retry roughly
    doubles them (bounded by the caps above). See the resource-label table in
    [Running on HPC / SLURM](usage.md#running-on-hpc).

## Tracing

| Parameter | Default | Description |
|---|---|---|
| `enable_trace` | `true` | Write Nextflow `trace.txt`, `report.html`, `timeline.html`, and per-task size logs. |
| `trace_dir` | `.trace` | Directory for trace outputs (independent of `--outdir`). |

## Incremental cyclic-IF mode (`add_cycle`)

Fold a **new imaging cycle** into an already-completed patient run, reusing the prior
reference, segmentation mask, and old-marker quantification instead of recomputing them.
Full walkthrough: [Incremental cycles](add_cycle.md).

| Parameter | Default | Description |
|---|---|---|
| `mode` | `standard` | `standard` = normal `--start`/`--stop` pipeline; `add_cycle` = incremental cyclic-IF. |
| `prior_outdir` | `null` | **Required for `add_cycle`.** The `--outdir` of the previously completed run (supplies the reusable reference, mask, and quantification via its checkpoint CSVs). |
| `embed_masks` | `false` | Embed the segmentation masks as a second uint32 series in the pyramid OME-TIFF. Written only when `embed_masks && quantify_compartments && expanded_quantification`; `add_cycle` consumes this series, so a prior run must have it to be extendable. `--embed_masks true` REQUIRES both `--quantify_compartments` and `--expanded_quantification` also true — the launch validation rejects the combination otherwise (see warning below). |

!!! warning "`add_cycle` prerequisites"
    `embed_masks` defaults to `false`, so a default run is **not** add_cycle-extendable.
    Set `--embed_masks true` (together with `--quantify_compartments` and
    `--expanded_quantification`, both on by default) to make a run extendable.
    `--embed_masks true` with either sibling off is rejected **at launch**
    (`ParamUtils.validateCompartmentQuant`) rather than silently producing a
    plain pyramid, so a prior run either failed to launch with `embed_masks=true`
    misconfigured, or has the mask series if `embed_masks=true` was accepted at
    all. Without the embedded mask series, `mode=add_cycle` **fast-fails** before
    doing any work. See
    [Incremental cycles → Fast-fail behavior](add_cycle.md#fast-fail-behavior).

## Parameter presets

Ready-made `-params-file` JSON presets in `params/`:

| Preset | Purpose |
|---|---|
| `params/full_pipeline.json` | All stages from preprocessing |
| `params/preprocessing_only.json` | Preprocessing only |
| `params/registration_only.json` | Registration from a checkpoint |
| `params/postprocessing_only.json` | Postprocessing from a checkpoint |
| `params/test.json` | Minimal test run |

```bash
nextflow run . -profile slurm,singularity \
  -params-file params/full_pipeline.json \
  --input samplesheet.csv --outdir results \
  --seg_method cellsam            # override any preset value on the CLI
```

## See also

- :material-console: **Command recipes & the samplesheet** — [Usage](usage.md)
- :material-help-circle: **Common questions** — [Usage → Troubleshooting & FAQ](usage.md#troubleshooting-faq)
- :material-book-open-variant: **How to cite** — [Citation](citation.md)
