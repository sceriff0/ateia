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
| `start` | `preprocessing` | Entry stage: `preprocessing`, `registration`, or `postprocessing`. |
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

VALIS whole-slide alignment.

### Common

| Parameter | Default | Description |
|---|---|---|
| `registration_method` | `valis` | Registration backend. Only `valis` is supported. |
| `allow_auto_reference` | `false` | If no `is_reference=true` row, use the first panel as reference. |
| `padding` | `false` | Pad all panels to the patient's max dimensions before registering. |
| `pad_mode` | `constant` | Padding fill mode: `constant`, `edge`, `reflect`, `symmetric`. |

### VALIS

| Parameter | Default | Description |
|---|---|---|
| `reg_reference_markers` | `['DAPI','FITC']` | Channels used to identify/align the reference. |
| `memory_mode` | `medium` | `low` (BRISK/RANSAC, small dims), `medium`, or `high` (SuperPoint/SuperGlue, larger dims). |
| `reg_micro_reg_fraction` | `0.125` | Image fraction used for micro-registration. |
| `reg_max_image_dim` | `4000` | Max cached image dimension during registration. |
| `skip_micro_registration` | `true` | Skip the micro-registration refinement step. |
| `reg_jvm_heap_gb` | `null` | Explicit JVM heap (GB) for VALIS. `null` auto-estimates from input size. |
| `reg_qc` | `1` | Registration QC depth: `0` = none, `1` = DAPI overlay only, `2` = DAPI overlay + segmentation-overlap metrics (Dice/IoU/instance-F1). |

### Distributed registration (advanced)

For very large slides, registration can be fanned out across tiles as separate
tasks instead of running through the in-process VALIS adapter. This is **off by
default** — the classic path is bit-identical and JVM-free for most inputs. Only
reach for these if a single REGISTER task can't fit a slide in memory.

| Parameter | Default | Description |
|---|---|---|
| `reg_distributed_tiling` | `false` | Master switch. `false` = classic in-process registration. |
| `reg_dist_sub_threshold` | `auto` | `auto` tiles only when VALIS would (estimated size > threshold); `force` always tiles. |
| `reg_dist_min_input_gb` | `1.0` | In `auto` mode, total full-res input ≥ this routes to distributed; smaller stays classic. |
| `reg_dist_force_tiling` | `false` | `false` = separated whole-image non-rigid (JVM-free); `true` = tiled fan-out. |
| `reg_dist_tile_wh` | `512` | Non-rigid tile size (px). |
| `reg_dist_micro_tile_wh` | `2048` | Micro-registration tile size (px). |
| `reg_dist_tile_buffer` | `100` | Tile overlap (px). |
| `reg_dist_threshold_gb` | `10` | Informational; matches VALIS's internal auto-tiler threshold. |
| `reg_dist_container` | *(GHCR image)* | Patched VALIS image used for distributed tiles. |
| `reg_max_non_rigid_dim` | `4096` | Non-rigid working resolution for the prep step. |

## Segmentation

Backend selected by `--seg_method`.

### Backend selection & shared

| Parameter | Default | Description |
|---|---|---|
| `seg_method` | `stardist` | Backend: `stardist`, `instantseg`, or `cellsam`. The container is chosen automatically. |
| `seg_gpu` | `true` | Use GPU (adds SLURM `--gres` + Singularity `--nv`). Set `false` to force CPU. |
| `seg_expand_distance` | `10` | Pixels to expand nuclei into the whole-cell mask (StarDist & CellSAM). |

### StarDist (`seg_method=stardist`)

Consumes the **DAPI channel** (must be channel 0 — guaranteed upstream).

| Parameter | Default | Description |
|---|---|---|
| `segmentation_model` | *(bundled)* | StarDist model name (`stardist_full_e200_…`). |
| `segmentation_model_dir` | `null` | Custom model directory; uses the bundled model if `null`. |
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
| `quantify_compartments` | `false` | Emit per-compartment signal (Nucleus / Cytoplasm / Cell) by routing the nuclear mask into quantification. |
| `expanded_quantification` | `false` | Also emit Median and Sum per compartment. **Requires** `quantify_compartments=true`. |

!!! danger "Validation rule"
    Setting `--expanded_quantification true` without `--quantify_compartments true`
    fails at launch with a clear error.

## Visualization & export

Pyramidal OME-TIFF assembly and GeoJSON.

| Parameter | Default | Description |
|---|---|---|
| `tilex` | `512` | Tile size (px, square) for the pyramidal OME-TIFF. |
| `pyramid_resolutions` | `8` | Number of pyramid resolution levels. |
| `pyramid_scale` | `2` | Downsampling factor between levels. |
| `compression` | `zstd` | Codec: `zstd`, `lzw`, `zlib`, `jpeg`, `none`. |

## Quality control & reports { #quality-control--reports }

| Parameter | Default | Description |
|---|---|---|
| `skip_preprocess_qc` | `false` | Skip preprocessing QC thumbnails. |
| `preprocess_qc_scale_factor` | `0.25` | Downsample factor for preprocessing QC images. |
| `skip_registration_qc` | `false` | Skip registration QC overlays. |
| `qc_scale_factor` | `0.25` | Downsample factor for registration QC images. |
| `skip_postprocessing_qc` | `false` | Skip segmentation/intensity QC plots. |
| `skip_final_qc_report` | `false` | Skip the aggregated HTML QC report. |
| `enable_feature_error` | `false` | Compute feature-based registration error → `feature_distances/`. |
| `feature_detector` | `superpoint` | Detector for error estimation: `superpoint`, `disk`, `dedode`, `brisk`, `vgg`. |
| `feature_max_dim` | `1024` | Max image dimension for feature detection. |
| `feature_n_features` | `5000` | Number of features to detect. |

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
