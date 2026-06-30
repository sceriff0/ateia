# Troubleshooting

This page lists the failure modes most commonly hit when running MIRAGE, and how to diagnose them. If your issue is not covered here, see the [FAQ](faq.md) or open an issue on the [GitHub tracker](https://github.com/sceriff0/mirage/issues) with the relevant Nextflow log and command line.

!!! tip "Jump to a category"

    <div class="grid cards" markdown>

    -   :material-update: __Environment & startup__

        ---

        [Nextflow version](#nextflow-version) ·
        [Hang at startup](#pipeline-appears-to-hang-at-startup) ·
        [Container backends](#container-backend-issues)

    -   :material-file-table: __Inputs & validation__

        ---

        [Invalid enums](#invalid-enum-values) ·
        [`--input` errors](#input-validation-errors) ·
        [Checkpoint CSVs](#where-are-my-checkpoint-csvs) ·
        [Bio-Formats](#bio-formats-image-format-issues)

    -   :material-memory: __Resources & HPC__

        ---

        [Out-of-memory](#out-of-memory-and-runtime-failures) ·
        [GPU](#gpu-not-detected-for-segmentation) ·
        [SLURM](#slurm-submission-errors)

    -   :material-vector-combine: __Stages__

        ---

        [VALIS registration](#valis-registration-failures) ·
        [Segmentation backends](#segmentation-backend-issues) ·
        [Quantification](#quantification-validation-errors)

    -   :material-bug: __Debugging__

        ---

        [Debug channels](#debug-channel-visibility) ·
        [Resetting a stuck run](#resetting-a-stuck-run) ·
        [Filing a bug report](#filing-a-bug-report)

    </div>

## Nextflow version

MIRAGE requires Nextflow `>=25.04.0` (DSL2) and Java 11 or newer. If a launch fails with

```
N E X T F L O W  ~  version <older>
ERROR ~ Unknown attribute ... / DSL ...
```

upgrade Nextflow:

```bash
nextflow self-update
nextflow -version
```

!!! note "On HPC modules"
    Where `nextflow` is provided as an environment module, load a recent build (`module load nextflow/25.04.0` or newer) or install a user-local copy. Confirm Java with `java -version`.

## Invalid enum values

The pipeline validates a small set of enums at startup via `lib/ParamUtils.groovy`:

- `--start` and `--stop` must be one of `preprocessing`, `registration`, `postprocessing`.
- `--registration_method` accepts only `valis` (legacy `gpu` and `cpu_tiled` methods are no longer supported).
- `--seg_method` must be one of `stardist` (default), `instantseg`, `cellsam`.

!!! success "Fail fast, fail clear"
    A typo exits **before any process is submitted** with a clear `Invalid --start` / `Invalid --registration_method` / `Invalid --seg_method` message. Run `--start X --stop X` to execute a single stage in isolation.

## `--input` validation errors { #input-validation-errors }

`--input` is required for all three entry points. The required CSV columns differ per `--start`:

| `--start` | Key column expected | Typical source |
|-----------|---------------------|----------------|
| `preprocessing` | `path_to_file` | your raw samplesheet |
| `registration` | `preprocessed_image` | `<outdir>/csv/preprocessed.csv` |
| `postprocessing` | `registered_image` | `<outdir>/csv/registered.csv` |

All three also require `patient_id`, `is_reference`, and `channels`. See the [Input Format](input_spec.md) page for the exact column schemas.

!!! warning "The most common mistake"
    Using the **preprocessing samplesheet** as input to `--start registration` (or any other stage mismatch). The column names will not match the stage's required schema and validation will reject the run. Always feed the checkpoint CSV that matches the stage you are resuming.

## Where are my checkpoint CSVs?

After each stage finishes, MIRAGE writes a single resume-ready CSV (covering **all** patients) into one `csv/` folder directly under `--outdir`:

```text
<outdir>/csv/preprocessed.csv     # written after preprocessing  → feeds --start registration
<outdir>/csv/registered.csv       # written after registration   → feeds --start postprocessing
<outdir>/csv/postprocessed.csv    # written after postprocessing
```

!!! danger "They are NOT under `<outdir>/<patient_id>/csv/`"
    A frequent source of confusion: people look for the checkpoint under the per-patient output tree and don't find it. The resume CSVs are aggregated, one file each, in `<outdir>/csv/` — not in the per-patient subtree.

Resume a later stage by pointing `--input` at the right file:

=== "Resume at registration"

    ```bash
    nextflow run sceriff0/mirage -profile docker \
      --input results/csv/preprocessed.csv \
      --start registration \
      --outdir results
    ```

=== "Resume at postprocessing"

    ```bash
    nextflow run sceriff0/mirage -profile docker \
      --input results/csv/registered.csv \
      --start postprocessing \
      --outdir results
    ```

See the [Restartability Guide](restartability_guide.md) for the full resume story.

## Out-of-memory and runtime failures

1. Raise the global caps: `--max_memory '700.GB'`, `--max_cpus 128`, `--max_time '240.h'`. The `local`, `test`, `test_full`, `slurm`, and `ieo` profiles turn these into `process.resourceLimits`, a hard ceiling that clamps every per-process request.
2. Per-process memory/time closures scale with `task.attempt`, so an **automatic retry raises** the request (clamped by the `resourceLimits` ceiling above). MIRAGE retries on exit codes `104,134,135,137,139,140,143`.
3. Inspect per-process settings in `conf/modules.config` and `conf/base.config` if a specific process needs a different baseline.
4. For registration specifically, drop to `--memory_mode low` and reduce `--feature_n_features` if VALIS runs out of RAM on the feature-detection pass (see [below](#out-of-memory-during-valis)).
5. Resume from the last successful task with `-resume` after adjusting params — Nextflow's cache means only the failing tasks re-execute.

!!! info "Exit codes worth knowing"
    `137` / `140` almost always mean the job was killed for exceeding its memory or time grant (OOM-killer or scheduler). `134`/`139` are aborts/segfaults inside a tool — check the task's `.command.log`.

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

!!! tip "The `ieo` profile does this for you"
    The `ieo` profile sets both cache variables automatically. For other sites, add them to your shell profile or a custom `site.config`. See [SLURM / HPC](slurm.md).

## Bio-Formats / image format issues

MIRAGE reads inputs through Bio-Formats (via the `CONVERT_IMAGE` module). Failures usually fall into three buckets:

!!! failure "Unsupported or corrupt vendor format"
    Check the [Bio-Formats supported formats list](https://bio-formats.readthedocs.io/en/stable/supported-formats.html). If your format is listed but still fails, the file may be corrupt or use a non-standard variant — open it in QuPath or Fiji first to confirm it reads at all.

!!! failure "Channel name mismatch"
    The CSV uses a pipe-separated `channels` column (e.g. `DAPI|CD3|CD8`). Each value must match a channel name embedded in the image's OME metadata. Use QuPath/Fiji or `bfconvert -info` to dump the actual channel names from the OME metadata and reconcile them with your samplesheet.

!!! failure "DAPI handling"
    DAPI **must be present** in `channels` (matched case-insensitively). `CONVERT_IMAGE` moves DAPI to channel 0 so downstream stages can rely on it. With `--preproc_skip_dapi true` (the default), DAPI bypasses BaSiC illumination correction. If you renamed DAPI to a custom label (e.g. `Hoechst`), the skip heuristic may miss it and correctness can suffer — keep the channel named `DAPI` or set `--preproc_skip_dapi false`.

## GPU not detected for segmentation

The `SEGMENT` module uses GPU when `--seg_gpu true`. If your environment lacks a CUDA-capable GPU or the driver is unavailable inside the container, segmentation falls back to (much slower) CPU. Force CPU mode explicitly if needed:

```bash
--seg_gpu false
```

For GPU runs:

- **Singularity** must pass through the device with `--nv` (the GPU-enabled profiles do this).
- **SLURM** jobs must actually request a GPU via `--gres=gpu:${gpu_type}`.
- Match `--gpu_type` to a valid generic-resource string from your cluster — list them with `sinfo -o "%G"`.

## Segmentation backend issues

Choose the backend with `--seg_method` (`stardist` default, `instantseg`, `cellsam`). See [Segmentation](segmentation.md) for a deeper comparison.

=== "StarDist (default)"

    StarDist requires **DAPI at channel 0**. This is guaranteed upstream by `CONVERT_IMAGE`, so a "wrong channel / nuclei not found" failure almost always means DAPI was missing or misnamed in `channels`. Confirm DAPI is present (case-insensitive) and that preprocessing completed before segmentation.

    !!! tip "Bring your own model"
        Point StarDist at a custom model with the StarDist model parameter (see [parameters.md](parameters.md) / [segmentation.md](segmentation.md)). Otherwise the bundled 2D fluorescence model is used.

=== "CellSAM"

    CellSAM locates DAPI **by name**, so the channel must be named `DAPI`. Its weights are auto-downloaded on first run, which requires a token and network access:

    ```bash
    export DEEPCELL_ACCESS_TOKEN=...   # required for weight download
    ```

    !!! warning "Offline / air-gapped clusters"
        If compute nodes have no internet, pre-download the weights on a login node and point the run at them:

        ```bash
        --cellsam_model_path /path/to/cellsam/weights
        ```

        A missing `DEEPCELL_ACCESS_TOKEN` on a node that *does* have network will surface as a download/auth error in the `SEGMENT` task's `.command.log`.

=== "InstanSeg"

    InstanSeg is **channel-invariant** (it does not care which channel DAPI is on). It caches its model under `instanseg_model_dir`; on offline clusters pre-populate that directory.

    !!! info "GPU OOM with InstanSeg"
        If InstanSeg runs out of GPU memory, lower the batch and tile size:

        ```bash
        --seg_instantseg_batch_size 1 \
        --seg_instantseg_tile_size 512
        ```

## Quantification validation errors

!!! failure "`--expanded_quantification requires --quantify_compartments`"
    Expanded (compartment-aware) quantification needs compartments to be enabled. Either pass `--quantify_compartments` alongside `--expanded_quantification`, or drop `--expanded_quantification` for a flat per-cell table. See [Quantification](quantification.md).

## VALIS registration failures

The only supported `--registration_method` is `valis`.

### Feature detection produces too few matches

If a slide has very low contrast or sparse structure, the detector returns too few keypoints and registration fails or produces a wildly incorrect alignment. Mitigations, in order of likelihood:

1. Raise `--feature_max_dim` (default 1024) so the detector sees a higher-resolution view and finds more keypoints. Lowering it makes the problem worse.
2. Raise `--feature_n_features` (try 5000) to allow more candidate keypoints.
3. Switch reference channels: `--reg_reference_markers '["DAPI"]'` is the most reliable on fluorescence data; add a structural marker (PANCK, SMA) if DAPI alone is weak.
4. Set `--memory_mode high` if you have the RAM — it relaxes downsampling.

!!! tip "Estimate distances first"
    The [Estimate Feature Distances](estimate_feature_distances.md) utility helps you sanity-check match quality before committing to a full run.

### Out-of-memory during VALIS

VALIS holds multiple registered images in memory. Drop to `--memory_mode low` and lower `--reg_max_image_dim`:

```bash
--memory_mode low \
--reg_max_image_dim 3000
```

VALIS auto-tiles non-rigid registration internally when estimated memory is high, so
there is no tile size to set. For low-RAM clusters, opt into the Nextflow-distributed
tiling path with `--reg_distributed_tiling true`.

### Micro-registration divergence

Micro-registration is **off by default** (`--skip_micro_registration true`). If you enabled it and the coarse alignment looked good but micro-registration overshoots, turn it back off:

```bash
--skip_micro_registration true
```

Then inspect the registration QC overlays to confirm. See [Registration Errors](registration_errors.md) for a deeper diagnostic guide.

## SLURM submission errors

- **Partition / QoS rejected.** Set `--slurm_partition`, `--slurm_account`, and `--slurm_qos` explicitly. The `ieo` profile pre-fills these for IEO; other sites need a `site.config` override.
- **GPU resource not available.** Match `--gpu_type` to your cluster — `sinfo -o "%G"` lists supported generic resources. The GPU request is emitted as `--gres=gpu:${gpu_type}`.
- **Job killed by scheduler.** Inspect `.nextflow.log` and the per-task `.command.log` under the work directory; SLURM cancellations usually correspond to time or memory limits being exceeded (exit `137`/`140`).

See [SLURM / HPC](slurm.md) for full cluster setup.

## Pipeline appears to hang at startup

The most common cause is Nextflow waiting for the container backend to pull a large image on the first run. Pre-pull the images once:

=== "Docker"

    ```bash
    docker pull bolt3x/attend_image_analysis:preprocess
    docker pull cdgatenbee/valis-wsi:1.0.0
    ```

=== "Singularity"

    ```bash
    singularity pull docker://bolt3x/attend_image_analysis:preprocess
    singularity pull docker://cdgatenbee/valis-wsi:1.0.0
    ```

The full container list is in `conf/modules.config`.

## Debug channel visibility

For channel-topology problems (the pipeline runs but produces zero outputs for some patients), enable verbose channel logging:

```bash
--debug_channels true
```

Subworkflows that support it will emit `.view` debug output showing the meta map at each junction. Combine with `-dump-channels` for a full topology dump on a dry run.

## Resetting a stuck run

If `-resume` keeps re-running the same failing task and you've already adjusted params, clear the Nextflow work cache for that **specific** task hash (visible in the error message):

```bash
rm -rf work/<hash-prefix>*
nextflow run ... -resume
```

!!! danger "Avoid the blanket nuke"
    Don't `rm -rf work/` unless you're prepared to recompute the entire pipeline. Clearing a single task hash is almost always what you want.

## Filing a bug report

A useful bug report includes:

- MIRAGE git commit (`git rev-parse HEAD`)
- Nextflow version (`nextflow -version`) and Java version (`java -version`)
- The exact command line, plus the `nextflow.config` profile combination used
- The relevant portion of `.nextflow.log` and the failing task's `.command.sh` and `.command.log`
- The first 200 lines of the input samplesheet (with patient IDs redacted if sensitive)

Open it on the [GitHub issue tracker](https://github.com/sceriff0/mirage/issues).
