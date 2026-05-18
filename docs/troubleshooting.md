# Troubleshooting

This page lists the failure modes most commonly hit when running MIRAGE, and how to diagnose them. If your issue is not covered here, open an issue on the [GitHub tracker](https://github.com/sceriff0/mirage/issues) with the relevant Nextflow log and command line.

## Nextflow version

MIRAGE requires Nextflow `>=25.04.0` (DSL2). If a launch fails with

```
N E X T F L O W  ~  version <older>
ERROR ~ Unknown attribute ... / DSL ...
```

upgrade Nextflow:

```bash
nextflow self-update
nextflow -version
```

On HPC systems where `nextflow` is provided as a module, load a recent build (`module load nextflow/25.04.0` or newer) or install a user-local copy.

## Invalid enum values

The pipeline validates a small set of enums at startup via `lib/ParamUtils.groovy`:

- `--start` and `--stop` must be one of `preprocessing`, `registration`, `postprocessing`.
- `--registration_method` accepts only `valis` (legacy `gpu` and `cpu_tiled` methods are no longer supported).

A typo here will exit before any process is submitted with a clear `Invalid --start` / `Invalid --registration_method` message. Run `--start X --stop X` to execute a single step in isolation.

## `--input` validation errors

`--input` is required for all three entry points. The required CSV columns differ per `--start`:

- `preprocessing` → samplesheet with raw images
- `registration` → CSV pointing at preprocessed OME-TIFFs (typically `results/<patient>/csv/preprocessed.csv`)
- `postprocessing` → CSV pointing at registered OME-TIFFs (typically `results/<patient>/csv/registered.csv`)

See the [Input Format](input_spec.md) page for the exact column schemas. The most common mistake is using the preprocessing samplesheet as input to `--start registration` — the column names will not match and validation will reject the run.

## Out-of-memory and runtime failures

1. Raise the global caps with `--max_memory '700.GB'`, `--max_cpus 128`, `--max_time '48.h'`.
2. Inspect the per-process settings in `conf/modules.config` and `conf/base.config`; resources scale with `task.attempt` via `check_max()`, so a single retry should already double memory.
3. For registration specifically, drop to `--memory_mode low` and reduce `--feature_n_features` if VALIS runs out of RAM on the feature-detection pass.
4. Resume from the last successful task with `-resume` after adjusting params — Nextflow's cache means only the failing tasks re-execute.

## Container backend issues

### Docker daemon not running

```
docker: Cannot connect to the Docker daemon
```

Start Docker Desktop (macOS/Windows) or `sudo systemctl start docker` (Linux), then re-run.

### Singularity / Apptainer cache permissions

On shared HPC systems, Singularity needs a writable cache directory. If you see

```
FATAL: container creation failed: ... permission denied
```

point the cache at a path you own:

```bash
export NXF_SINGULARITY_CACHEDIR=$HOME/.singularity_cache
export SINGULARITY_CACHEDIR=$HOME/.singularity_cache
mkdir -p "$NXF_SINGULARITY_CACHEDIR"
```

The `ieo` profile sets these automatically; for other sites, add them to your shell profile or a custom `site.config`.

## Bio-Formats / image format issues

MIRAGE reads inputs through Bio-Formats (via the `CONVERT_IMAGE` module). Failures usually fall into:

- **Unsupported vendor format.** Check the [Bio-Formats supported formats list](https://bio-formats.readthedocs.io/en/stable/supported-formats.html). If your format is listed but fails, the file may be corrupt or use a non-standard variant — open it in QuPath or Fiji first to confirm.
- **Channel name mismatch.** The CSV column `channel_name` must match the channel names embedded in the image metadata exactly (case-sensitive). Use `bin/ome_tiff_inspect.py` (or `tifffile`/`bfconvert -info`) to dump the actual channel names from the OME metadata.
- **DAPI handling.** With `--preproc_skip_dapi true` (the default), DAPI channels bypass illumination correction. If you renamed DAPI to a custom label (e.g. `Hoechst`), set `--preproc_skip_dapi false` or rename the channel.

## GPU not detected for StarDist

The segmentation module prefers GPU. If your environment lacks a CUDA-capable GPU or the driver is unavailable inside the container, segmentation will fall back to CPU and run dramatically slower. Force CPU mode explicitly:

```bash
--seg_gpu false
```

For SLURM, ensure the job actually requests a GPU. The pipeline expects `--gpu_type` to be a valid SLURM generic-resource string (default `nvidia_h200:1`) — adjust to whatever your cluster exposes via `sinfo -o "%G"`.

## VALIS registration failures

### Feature detection produces too few matches

VALIS uses learned feature detectors (default: SuperPoint). If a slide has very low contrast or sparse structure, the detector returns too few keypoints and registration fails or produces a wildly incorrect alignment. Mitigations, in order of likelihood:

1. Lower `--feature_max_dim` (default 1024) so the detector sees a higher-resolution downscale.
2. Raise `--feature_n_features` to allow more candidate keypoints.
3. Switch reference channels: `--reg_reference_markers '["DAPI"]'` is the most reliable on fluorescence data; add a structural marker (PANCK, SMA) if DAPI alone is weak.
4. Set `--memory_mode high` if you have the RAM — it relaxes downsampling.

### Out-of-memory during VALIS

VALIS holds multiple registered images in memory. Drop to `--memory_mode low`, lower `--reg_max_image_dim`, and enable tiled registration: `--reg_use_tiled_registration true --reg_tile_size 4096`.

### Micro-registration divergence

If the coarse alignment looks good but micro-registration overshoots, disable it: `--skip_micro_registration true`. Then inspect the registration QC overlays under `results/<patient>/registered/qc/` to confirm.

## SLURM submission errors

- **Partition / QoS rejected.** Set `--slurm_partition`, `--slurm_account`, and `--slurm_qos` explicitly. The `ieo` profile pre-fills these for IEO; other sites need a `site.config` override.
- **GPU resource not available.** Match `--gpu_type` to your cluster — `sinfo -o "%G"` lists supported generic resources.
- **Job killed by scheduler.** Inspect `.nextflow.log` and the per-task `.command.log` under the work directory; SLURM cancellations usually correspond to time or memory limits being exceeded.

## Pipeline appears to hang at startup

The most common cause is Nextflow waiting for the container backend to pull a large image on the first run. Pre-pull the images once:

```bash
# Docker
docker pull bolt3x/attend_image_analysis:preprocess
docker pull cdgatenbee/valis-wsi:1.0.0
# Singularity
singularity pull docker://bolt3x/attend_image_analysis:preprocess
```

The full container list is in `conf/modules.config`.

## Debug channel visibility

For channel-topology problems (the pipeline runs but produces zero outputs for some patients), enable verbose channel logging:

```bash
--debug_channels true
```

Subworkflows that support it will emit `.view` debug output showing the meta map at each junction. Combine with `-dump-channels` for a full topology dump on dry-run.

## Resetting a stuck run

If `-resume` keeps re-running the same failing task and you've already adjusted params, clear the Nextflow work cache for that specific task hash (visible in the error message) — `rm -rf work/<hash-prefix>*` — then `-resume` again. Avoid blanket `rm -rf work/` unless you're prepared to recompute the entire pipeline.

## Filing a bug report

A useful bug report includes:

- MIRAGE git commit (`git rev-parse HEAD`)
- Nextflow version (`nextflow -version`)
- The exact command line, plus the `nextflow.config` profile combination used
- The relevant portion of `.nextflow.log` and the failing task's `.command.sh` and `.command.log`
- The first 200 lines of the input samplesheet (with patient IDs redacted if sensitive)
