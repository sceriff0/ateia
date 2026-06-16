# Frequently Asked Questions

Short, practical answers to the questions users ask most. Click a question to expand it. For step-by-step diagnosis of a specific failure, see [Troubleshooting](troubleshooting.md).

!!! tip "How to use this page"
    Each entry is collapsible and grouped by theme. Use your browser's find (`Ctrl/Cmd+F`) to jump to a keyword like *DAPI*, *resume*, or *GPU*.

## Getting started

??? question "What does MIRAGE actually do?"
    MIRAGE is a Nextflow DSL2 pipeline for whole-slide image (WSI) processing. It runs three stages — **preprocessing → registration → postprocessing** (segmentation, quantification, and GeoJSON export) — producing per-cell measurements you can open in downstream tools. Start at the [Workflow overview](workflow.md) and the [Walkthrough](walkthrough.md).

??? question "How do I run my first pipeline?"
    The quickest path is the test profile in stub mode, which exercises the wiring without heavy compute:

    ```bash
    nextflow run sceriff0/mirage -profile test,docker -stub --outdir results
    ```

    Then follow [Getting Started](getting_started.md) and the [Walkthrough](walkthrough.md) with your own data.

??? question "What are the system requirements?"
    Nextflow `>=25.04.0`, Java 11+, and a container engine (Docker, Singularity/Apptainer). A CUDA GPU is optional but speeds up segmentation. See [Installation](installation.md).

??? question "Do I need a GPU?"
    No — MIRAGE runs CPU-only with `--seg_gpu false`. A GPU mainly accelerates the `SEGMENT` step. If you have one, set `--seg_gpu true` and make sure the container sees the device (`--nv` for Singularity, `--gres=gpu:${gpu_type}` on SLURM). See [Segmentation](segmentation.md) and [Troubleshooting → GPU](troubleshooting.md#gpu-not-detected-for-segmentation).

??? question "Can I run without containers?"
    MIRAGE is designed around containers — every process declares one (`bolt3x/attend_image_analysis:*` and `cdgatenbee/valis-wsi:1.0.0`). Running bare-metal is not a supported configuration; use Docker or Singularity/Apptainer. See [Installation](installation.md).

## Inputs & samplesheets

??? question "What does `--input` need to contain?"
    A CSV samplesheet. The required columns depend on where you start. Every stage needs `patient_id`, `is_reference`, and `channels`; additionally:

    - `preprocessing` needs `path_to_file`
    - `registration` needs `preprocessed_image`
    - `postprocessing` needs `registered_image`

    Full schemas are in [Input Format](input_spec.md).

??? question "What image formats are supported?"
    Anything Bio-Formats can read is fed through the `CONVERT_IMAGE` module. Check the [Bio-Formats supported formats list](https://bio-formats.readthedocs.io/en/stable/supported-formats.html). If a listed format fails, the file may be corrupt or a non-standard variant — open it in QuPath/Fiji first. See [Troubleshooting → Bio-Formats](troubleshooting.md#bio-formats-image-format-issues).

??? question "What does `is_reference` mean?"
    It marks which image per patient is the **fixed reference** that the others register *onto*. Exactly one image per patient should be the reference; the rest are moving images aligned to it. See [Input Format](input_spec.md) and [Registration Methods](registration_methods.md).

??? question "Why must DAPI be present in `channels`?"
    DAPI is the nuclear anchor MIRAGE relies on. `CONVERT_IMAGE` moves DAPI to channel 0 so that StarDist (which needs DAPI at channel 0) works out of the box, and DAPI is the most reliable registration reference on fluorescence data. DAPI is matched case-insensitively in `channels`. If you renamed it (e.g. `Hoechst`), correctness may suffer — see the next question.

??? question "I renamed DAPI to Hoechst — will it still work?"
    Possibly not optimally. With `--preproc_skip_dapi true` (the default), DAPI bypasses BaSiC illumination correction; the skip is keyed on the DAPI name, so a renamed channel may be corrected anyway and CellSAM (which finds DAPI by name) may not locate it. Keep the channel named `DAPI`, or set `--preproc_skip_dapi false` and use a backend that doesn't depend on the name (InstanSeg is channel-invariant).

??? question "How do I check the channel names in my image?"
    Open it in QuPath or Fiji, or dump the OME metadata with `bfconvert -info`. The names you put in the `channels` column must reconcile with what's embedded in the file. See [Troubleshooting → Channel name mismatch](troubleshooting.md#bio-formats-image-format-issues).

??? question "How do I validate my samplesheet without running the whole pipeline?"
    Run a single stage in stub mode so validation and channel wiring execute without heavy compute:

    ```bash
    nextflow run sceriff0/mirage -profile docker -stub \
      --input my_samplesheet.csv --start preprocessing --stop preprocessing --outdir results
    ```

    Enum and `--input` column validation happen at startup via `lib/ParamUtils.groovy`, so schema problems fail fast. See [Usage](usage.md).

## Running & resuming

??? question "How do I run a single stage?"
    Set `--start` and `--stop` to the same stage. For example, registration only:

    ```bash
    nextflow run sceriff0/mirage -profile docker \
      --input csv/preprocessed.csv --start registration --stop registration --outdir results
    ```

    Both `--start` and `--stop` accept `preprocessing`, `registration`, or `postprocessing`. See [Restartability Guide](restartability_guide.md).

??? question "If I omit `--stop`, what happens?"
    The pipeline runs from `--start` all the way to the end (postprocessing). `--stop` only matters when you want to halt early.

??? question "Where are the checkpoint CSVs?"
    In a `csv/` folder in your **launch directory** (where you ran `nextflow run`), **not** under `--outdir`:

    ```text
    ./csv/preprocessed.csv
    ./csv/registered.csv
    ./csv/postprocessed.csv
    ```

    Each is a single file covering all patients. They are **not** under `<outdir>/<patient>/csv/`. See [Troubleshooting → Where are my checkpoint CSVs](troubleshooting.md#where-are-my-checkpoint-csvs).

??? question "How do I resume just postprocessing?"
    Feed the registration checkpoint and start there:

    ```bash
    nextflow run sceriff0/mirage -profile docker \
      --input csv/registered.csv --start postprocessing --outdir results
    ```

    See [Restartability Guide](restartability_guide.md).

??? question "What's the difference between `--start` and `-resume`?"
    They solve different problems:

    - `-resume` is Nextflow's **cache** — re-running the same command reuses completed tasks and only recomputes what changed.
    - `--start` chooses **which stage** the pipeline begins at, reading a checkpoint CSV. Use it to skip stages you've already finished in a *previous* launch.

    You often use both: `--start postprocessing ... -resume`.

??? question "Nextflow keeps re-running the same failed task even with `-resume`. What now?"
    Clear that one task's cache by hash (shown in the error) and resume again:

    ```bash
    rm -rf work/<hash-prefix>*
    nextflow run ... -resume
    ```

    Avoid `rm -rf work/` wholesale. See [Troubleshooting → Resetting a stuck run](troubleshooting.md#resetting-a-stuck-run).

??? question "The run seems to hang right after starting. Is it broken?"
    Usually it's pulling large container images on first run. Pre-pull them once with `docker pull` / `singularity pull`. See [Troubleshooting → Hang at startup](troubleshooting.md#pipeline-appears-to-hang-at-startup).

## Segmentation & quantification

??? question "Which segmentation backend should I use?"
    Pick with `--seg_method`:

    | Backend | Notes |
    |---------|-------|
    | `stardist` (default) | Robust nuclear segmentation; needs DAPI at channel 0 (guaranteed upstream). A good default. |
    | `instantseg` | Channel-invariant; tune batch/tile size for GPU memory. |
    | `cellsam` | Finds DAPI by name; needs weight download (token/network) or a local model path. |

    See [Segmentation](segmentation.md) and [Troubleshooting → Segmentation backends](troubleshooting.md#segmentation-backend-issues).

??? question "Can I use my own StarDist model?"
    Yes — point StarDist at a custom model path (see [parameters.md](parameters.md) / [segmentation.md](segmentation.md)). Without one, the bundled 2D fluorescence model is used.

??? question "CellSAM fails to download its weights. How do I fix it?"
    On a node with internet, export a DeepCell token before the run:

    ```bash
    export DEEPCELL_ACCESS_TOKEN=...
    ```

    On offline/air-gapped clusters, pre-download the weights and pass `--cellsam_model_path /path/to/weights`. See [Troubleshooting → CellSAM](troubleshooting.md#segmentation-backend-issues).

??? question "InstanSeg runs out of GPU memory. What can I lower?"
    Reduce the batch and tile size:

    ```bash
    --seg_instantseg_batch_size 1 --seg_instantseg_tile_size 512
    ```

    On offline clusters, pre-populate the model cache directory (`instanseg_model_dir`). See [Segmentation](segmentation.md).

??? question "What is compartment quantification?"
    Compartment-aware quantification splits per-cell measurements by subcellular compartment (e.g. nucleus vs. surrounding). Enable it with `--quantify_compartments`. The **expanded** output (`--expanded_quantification`) builds on it — and *requires* it. See [Quantification](quantification.md).

??? question "I got `--expanded_quantification requires --quantify_compartments`. What's wrong?"
    Expanded quantification depends on compartments. Either add `--quantify_compartments`, or drop `--expanded_quantification` for a flat per-cell table. See [Troubleshooting → Quantification](troubleshooting.md#quantification-validation-errors).

??? question "Does MIRAGE do phenotyping or cell typing?"
    No. MIRAGE stops at segmentation, quantification, and GeoJSON/feature export. Phenotyping and **gating are downstream**, in tools like QuPath or FlowPath. See [Export](export.md) and the next section.

## HPC & resources

??? question "How do I run on a SLURM cluster?"
    Use a SLURM-aware profile (the `ieo` profile is a worked example) or supply `--slurm_partition`, `--slurm_account`, and `--slurm_qos`. See [SLURM / HPC](slurm.md).

??? question "Singularity fails with `FATAL: ... permission denied`. Why?"
    The Singularity cache isn't writable. Point it at a path you own:

    ```bash
    export NXF_SINGULARITY_CACHEDIR=$HOME/.singularity_cache
    export SINGULARITY_CACHEDIR=$HOME/.singularity_cache
    ```

    The `ieo` profile sets these automatically. See [Troubleshooting → Container backends](troubleshooting.md#container-backend-issues).

??? question "How do I reduce memory use in registration?"
    Drop to `--memory_mode low`, lower `--reg_max_image_dim`, and enable tiled registration with `--reg_use_tiled_registration true --reg_tile_size 4096`. If the feature-detection pass is the culprit, also lower `--feature_n_features`. See [Registration Errors](registration_errors.md).

??? question "A job died with exit code 137. What does that mean?"
    `137` (and `140`) almost always means the job was killed for exceeding its memory or time grant. MIRAGE auto-retries on `104,134,135,137,139,140,143`, and each retry scales memory/time with `task.attempt`. Raise the global caps with `--max_memory '700.GB'`, `--max_cpus 128`, `--max_time '240.h'` if needed (the `local`, `test`, `test_full`, `slurm`, and `ieo` profiles clamp every request to these via `process.resourceLimits`). See [Troubleshooting → OOM](troubleshooting.md#out-of-memory-and-runtime-failures).

??? question "What are the global resource caps?"
    `--max_memory` defaults to `700.GB`, `--max_cpus` to `128`, `--max_time` to `240.h`. The `local`, `test`, `test_full`, `slurm`, and `ieo` profiles set `process.resourceLimits` from these, clamping every per-process request to the ceiling.

??? question "How many patients can run at once?"
    There's no fixed limit — MIRAGE uses streaming `groupTuple` with patient/channel counts pre-computed from the CSV, so patients flow through concurrently as resources allow. Throughput is bounded by your executor's queue limits and the resource caps above. See [Workflow](workflow.md).

??? question "GPU jobs aren't getting a GPU on SLURM. What's missing?"
    The job must request one with `--gres=gpu:${gpu_type}`, and `--gpu_type` must match a real generic resource on your cluster — list them with `sinfo -o "%G"`. Singularity also needs `--nv`. See [Troubleshooting → SLURM](troubleshooting.md#slurm-submission-errors).

## Outputs & visualization

??? question "Where do my results go?"
    Under `--outdir`, organized per patient and per stage. The structure and file meanings are documented in [Outputs](outputs.md).

??? question "How do I open results in QuPath?"
    MIRAGE exports GeoJSON (and OME-TIFFs from earlier stages) that QuPath imports directly — load the registered image and overlay the exported cell objects. See [Export](export.md) and [Outputs](outputs.md).

??? question "What are the trace/report/timeline files?"
    With `enable_trace` (default `true`), Nextflow writes `trace.txt`, `report.html`, and `timeline.html` to `--trace_dir` (default `.trace`). They're handy for spotting which step is slow or memory-hungry.

## Troubleshooting & meta

??? question "How do I debug channel-topology problems (some patients produce no output)?"
    Turn on verbose channel logging with `--debug_channels true`, and add `-dump-channels` for a topology dump on a dry run. See [Troubleshooting → Debug channels](troubleshooting.md#debug-channel-visibility).

??? question "How do I file a good bug report?"
    Include the MIRAGE commit (`git rev-parse HEAD`), `nextflow -version`, the exact command line and profile, the relevant `.nextflow.log` excerpt, and the failing task's `.command.sh`/`.command.log`. Open it on the [issue tracker](https://github.com/sceriff0/mirage/issues). See [Troubleshooting → Filing a bug report](troubleshooting.md#filing-a-bug-report).

??? question "How do I run the test suite?"
    nf-test and Python unit tests are documented in the [Testing Guide](testing_guide.md). For contributing changes, see the [Developer Guide](developer_guide.md).

??? question "How do I cite MIRAGE?"
    See the [Citation](citation.md) page for the preferred reference.

---

!!! question "Still stuck?"
    Work through the matching section of the [Troubleshooting](troubleshooting.md) page, and if that doesn't resolve it, open an issue with your logs and command line on the [GitHub tracker](https://github.com/sceriff0/mirage/issues).
