# Parameters

This page documents the active canonical parameter surface.

## Canonical Sources

- Defaults: `nextflow.config` (`params { ... }`)
- Validation/enums: `lib/ParamUtils.groovy`
- Schema: `nextflow_schema.json`

## High-Impact Parameters

| Parameter | Default | Notes |
|---|---|---|
| `input` | `''` | Required for `preprocessing`, `registration`, `postprocessing` |
| `outdir` | _(required)_ | Output root directory. No default — pass via `--outdir` or set in a profile. |
| `savedir` | `null` | Optional archive destination |
| `start` | `preprocessing` | Pipeline entry point: `preprocessing`, `registration`, `postprocessing` |
| `stop` | `null` | Pipeline step to stop after. Default: null (runs to end). Values: `preprocessing`, `registration`, `postprocessing` |
| `debug_channels` | `false` | Enables debug channel `.view` output |
| `dry_run` | `false` | Validation-only mode |

## Preprocessing

- `preproc_tile_size`
- `preproc_skip_dapi`
- `preproc_autotune`
- `preproc_n_iter`
- `preproc_pool_workers`
- `preproc_overlap`
- `preproc_no_darkfield`
- `preprocess_qc_scale_factor`

## Registration

- Common:
  - `allow_auto_reference`
  - `padding`
  - `pad_mode`
  - `skip_registration_qc`
  - `qc_scale_factor`
  - `enable_feature_error`
  - `feature_detector`
  - `feature_max_dim`
  - `feature_n_features`
- VALIS:
  - `reg_reference_markers`
  - `memory_mode`
  - `reg_micro_reg_fraction`
  - `reg_max_image_dim`
  - `skip_micro_registration`
  - `reg_parallel_warping`
  - `reg_n_workers`
  - `reg_use_tiled_registration`
  - `reg_tile_size`
## Postprocessing

- Segmentation:
  - `seg_method` — backend to use: `'stardist'` (default, DAPI-channel only), `'instantseg'` (channel-invariant, consumes the multichannel image directly), or `'cellsam'` (CellSAM SAM foundation model on the DAPI channel). The container image is selected automatically based on this value.
  - `seg_gpu`
  - `seg_pmin` (StarDist only)
  - `seg_pmax` (StarDist only)
  - `seg_n_tiles_y` (StarDist only)
  - `seg_n_tiles_x` (StarDist only)
  - `seg_expand_distance` (StarDist and CellSAM — distance used to expand nuclei labels into the whole-cell mask)
  - `segmentation_model_dir` (StarDist only)
  - `segmentation_model` (StarDist only)
  - `seg_instantseg_model` — InstanSeg pretrained model name. Default `'fluorescence_nuclei_and_cells'`.
  - `seg_instantseg_target` — `'all_outputs'` (default; nuclei + cells), `'cells'`, or `'nuclei'`. When a single target is produced, that mask is replicated to both `_nuclei_mask.tif` and `_cell_mask.tif` so downstream stays contract-preserving.
  - `seg_cellsam_bbox_threshold` (CellSAM only) — bounding-box confidence threshold; the main precision/recall knob. Default `0.4`.
  - `seg_cellsam_use_wsi` (CellSAM only) — enable CellSAM's native whole-slide tiling for large images. Default `true`.
  - `seg_cellsam_block_size` (CellSAM only) — tile side length (px) in WSI mode; recommended range `[256, 2048]`. Default `400`.
  - `seg_cellsam_overlap` (CellSAM only) — tile overlap (px) in WSI mode. Default `56`.
  - `cellsam_model_path` (CellSAM only) — path to pre-downloaded CellSAM weights for offline/container use. When `null`, weights are auto-downloaded at runtime — `export DEEPCELL_ACCESS_TOKEN=...` before launching and the pipeline forwards it into the container automatically (clusters without compute-node internet should pre-download weights and set `cellsam_model_path` instead). CellSAM segments the DAPI channel (located by name from the `channels` column, not assumed to be channel 0) to produce nuclei; the whole-cell mask is derived by expanding nuclei labels via `seg_expand_distance` (StarDist-style).
- Quantification:
  - `quant_min_area`
- Pyramid export:
  - `pixel_size`
  - `tilex`
  - `tiley`
  - `pyramid_resolutions`
  - `pyramid_scale`
  - `compression`

## Infrastructure and Limits

- Cluster/runtime:
  - `slurm_partition`
  - `slurm_account`
  - `slurm_qos`
  - `gpu_type`
  - `publish_dir_mode`
- Limits:
  - `max_memory`
  - `max_cpus`
  - `max_time`
- Trace:
  - `enable_trace`
  - `trace_dir`

