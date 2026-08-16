# Installation

This page gets MIRAGE running on your machine — laptop, workstation, or HPC login node. The whole setup is: install **Nextflow**, pick **one container backend**, clone the repo, and verify. Budget about 10–15 minutes, most of it spent pulling container images on the first run.

!!! tip "In a hurry?"
    Already have Nextflow and Docker? Skip to [Clone the repository](#clone-the-repository), then head straight to the [Walkthrough](usage.md).

## Requirements

| Requirement | Version / detail | Notes |
|---|---|---|
| **Nextflow** | `>=25.04.0` | Uses the `nf-schema@2.5.1` plugin; older versions will not run. |
| **Java** | 11 or newer | Required by Nextflow. Check with `java -version`. |
| **Container backend** | one of Singularity/Apptainer, Docker, or Conda | Singularity recommended on HPC; Docker for local/dev. |
| **Free disk** | ~10 GB | For container images plus intermediate outputs. |
| **Network access on first run** | outbound access to the Nextflow plugin registry | Nextflow fetches `nf-schema` from the registry the first time the pipeline runs. On a network-isolated compute node, pre-provision the plugin first — see [Offline / air-gapped execution](usage.md#offline-air-gapped-execution). |

!!! warning "Nextflow version matters"
    MIRAGE requires Nextflow **25.04.0 or later**. If `nextflow -version` reports something older, update it (next section) before doing anything else.

## Install Nextflow

If you don't have Nextflow yet:

```bash
curl -s https://get.nextflow.io | bash
# move the launcher onto your PATH
sudo mv nextflow /usr/local/bin/
```

If you already have it but it's too old, self-update in place:

```bash
nextflow self-update
```

!!! info "Java first"
    Nextflow needs Java 11+. On most clusters this is a module (`module load java`); on a Mac, `brew install openjdk@17` works well.

## Choose a container backend

Every MIRAGE process runs inside a container with a pinned version tag — you don't install the scientific tools (VALIS, StarDist, Bio-Formats, …) yourself. Pick the backend that matches where you're running.

=== "Docker"

    Best for **local development and laptops**. Make sure the Docker daemon is running, then add `docker` to your profile:

    ```bash
    nextflow run . --input samplesheet.csv --outdir results -profile docker
    ```

    !!! tip
        On Docker Desktop, give the VM enough memory (8 GB+) in **Settings → Resources** so segmentation and registration don't get OOM-killed.

=== "Singularity / Apptainer"

    **Recommended on HPC**, where Docker is usually unavailable or disallowed. Singularity runs rootless and plays well with shared filesystems:

    ```bash
    nextflow run . --input samplesheet.csv --outdir results -profile singularity
    ```

    Apptainer (the renamed successor to Singularity) is a drop-in replacement and uses the same `singularity` profile.

    !!! tip "Cache images once"
        Point `NXF_SINGULARITY_CACHEDIR` at a shared, persistent path so images are pulled once and reused across runs and users:

        ```bash
        export NXF_SINGULARITY_CACHEDIR=/path/to/shared/singularity_cache
        ```

=== "Conda"

    A containerless option using Conda-managed environments. Slower to set up and less reproducible than containers, but useful where neither Docker nor Singularity is available:

    ```bash
    nextflow run . --input samplesheet.csv --outdir results -profile conda
    ```

!!! note "Combine profiles"
    Container profiles combine with execution profiles via commas: `-profile slurm,singularity` on a cluster, `-profile test,docker` for the bundled demo, `-profile local,docker` for a capped local run.

## Clone the repository

```bash
git clone https://github.com/sceriff0/mirage.git
cd mirage
```

All commands on this site assume you run them from the repo root (`nextflow run .`).

## Verify

Confirm Nextflow is present and new enough:

```bash
nextflow -version
```

You should see version **25.04.0 or later**. A quick stub run validates the whole wiring without pulling heavy images or running real tools:

```bash
nextflow run . -profile test,docker -stub --outdir results_stub
```

If that completes without `FAILED` processes, your installation is sound. (You'll need the test data generated first for a *real* run — see the [Walkthrough](usage.md).)

## Pre-pulling container images (optional)

The first real run downloads each tool's image, which can take several minutes. Pull them ahead of time to make that first run snappy. MIRAGE uses these images:

| Image | Used for |
|---|---|
| `bolt3x/mirage-*` | preprocessing, segmentation, quantification, export |
| `cdgatenbee/valis-wsi:1.0.0` | VALIS registration |
| `ubuntu:22.04` | size-log aggregation (`AGGREGATE_SIZE_LOGS`) |

=== "Docker"

    ```bash
    docker pull cdgatenbee/valis-wsi:1.0.0
    # pull the mirage-<component> image your config pins (check conf/modules.config)
    docker pull bolt3x/mirage-<component>:1.0.0
    ```

=== "Singularity / Apptainer"

    ```bash
    singularity pull docker://cdgatenbee/valis-wsi:1.0.0
    singularity pull docker://bolt3x/mirage-<component>:1.0.0
    ```

!!! note "Where tags live"
    The exact `bolt3x/mirage-<component>` image and version are pinned per-process in `modules/local/*.nf` (and resource overrides in `conf/modules.config`). MIRAGE never uses `:latest`. Each image has its own Docker Hub repository and an immutable version tag matching `manifest.version` (currently `1.0.0`) — see [`containers/README.md`](https://github.com/sceriff0/mirage/blob/main/containers/README.md).

## GPU notes

Segmentation can run on a GPU for a substantial speedup, or fall back to CPU.

- **Enable GPU**: `seg_gpu = true`. Your container runtime must expose the GPU — Singularity needs `--nv`, and on SLURM you must request a GPU (e.g. `--gres=gpu:1`). See the [SLURM guide](usage.md#running-on-hpc) for cluster specifics.
- **CPU fallback**: `seg_gpu = false`. Slower but works everywhere — this is what the `test` profile uses so the demo runs on any laptop.

!!! warning
    If you request `seg_gpu = true` without exposing a GPU to the container, segmentation will fail or silently fall back depending on the backend. When in doubt on a new machine, start with `seg_gpu = false`.

## Building the docs locally (optional)

Contributing to this site? The docs are a MkDocs Material site. Build and live-preview them:

```bash
pip install -r docs/requirements.txt
mkdocs serve
```

Then open <http://127.0.0.1:8000> — the site rebuilds on every save.

## Execution profiles overview

Profiles are defined in `nextflow.config` and combined with commas. Pick one **execution** profile and one **container** profile.

| Profile | Kind | What it does |
|---|---|---|
| `test` | data + caps | Bundled synthetic dataset, small resource caps, segmentation on CPU. |
| `test_full` | data + caps | Larger synthetic dataset with more realistic parameters. |
| `instantseg_test` | data + caps | Test profile exercising the InstanSeg segmentation backend. |
| `cellsam_test` | data + caps | Test profile exercising the CellSAM segmentation backend. |
| `docker` | container | Run processes in Docker containers (local/dev). |
| `singularity` | container | Run processes in Singularity/Apptainer containers (recommended on HPC). |
| `conda` | container | Run with Conda-managed environments (no containers). |
| `slurm` | executor | Submit each process as a SLURM job. |
| `local` | executor | Local executor with conservative caps (4 CPU / 16 GB). |
| `ieo` | site | IEO cluster profile (gitignored; site-specific). |

!!! example "Common combinations"
    - Laptop demo: `-profile test,docker`
    - Local capped run on your data: `-profile local,docker`
    - HPC production run: `-profile slurm,singularity`

## Next steps

Head to **[Usage](usage.md)** — the four-stage model, the samplesheet, the
end-to-end run on synthetic data, resuming from checkpoints, and what lands in
`--outdir`. To tune the run, see **[Parameters](parameters.md)**.
