# CLI & Usage

This is the practical command reference for running MIRAGE. For the full parameter surface see [Parameters](parameters.md); for HPC submission see [Running on SLURM](slurm.md); for resuming from checkpoints see [Restartability & Checkpoints](restartability_guide.md).

!!! note "Two kinds of flags"
    Nextflow distinguishes **pipeline parameters** (double dash, `--input`) from **Nextflow options** (single dash, `-profile`, `-resume`, `-c`, `-params-file`). Both appear on the same command line.

---

## Runtime flags

| Flag | Kind | Required | Description |
|---|---|:---:|---|
| `--input` | param | yes | Path to the samplesheet CSV. Columns depend on `--start` — see [Input Format](input_spec.md). |
| `--outdir` | param | yes | Output root directory. Results, QC, and checkpoints reference paths here. |
| `--start` | param | no | Entry stage: `preprocessing` (default), `registration`, or `postprocessing`. |
| `--stop` | param | no | Last stage to run. Omitted = run to the end. Same three values. |
| `--savedir` | param | no | Optional archive location; finalized results are also copied here. |
| `--dry_run true` | param | no | Validate inputs and the samplesheet, then exit without running tasks. |
| `-profile` | option | no | Execution/config profiles, comma-combined (e.g. `docker,test`). |
| `-params-file` | option | no | JSON preset of parameters, e.g. `params/full_pipeline.json`. |
| `-resume` | option | no | Reuse cached results from a previous run's `work/` directory. |
| `-c` | option | no | Layer an extra config file (e.g. site-specific SLURM settings). |

!!! info "Stage selection"
    The three stages run in order: **preprocessing → registration → postprocessing**. A stage executes only when `start_idx ≤ stage_idx ≤ stop_idx`. So `--start registration --stop registration` runs **only** registration; `--start preprocessing` with no `--stop` runs everything.

---

## Common invocations

=== "Full pipeline"

    Raw images all the way through to GeoJSON export.

    ```bash
    nextflow run main.nf \
      --input samplesheet.csv \
      --outdir results \
      --start preprocessing \
      -profile docker \
      -params-file params/full_pipeline.json
    ```

=== "Single stage"

    Run exactly one stage with `--start X --stop X`.

    ```bash
    nextflow run main.nf \
      --input samplesheet.csv \
      --outdir results \
      --start preprocessing --stop preprocessing \
      -profile docker
    ```

=== "Resume at registration"

    Feed the checkpoint that preprocessing wrote to `csv/`.

    ```bash
    nextflow run main.nf \
      --input csv/preprocessed.csv \
      --outdir results \
      --start registration \
      -profile docker \
      -resume
    ```

=== "Resume at postprocessing"

    Feed the checkpoint that registration wrote to `csv/`.

    ```bash
    nextflow run main.nf \
      --input csv/registered.csv \
      --outdir results \
      --start postprocessing \
      -profile docker \
      -resume
    ```

=== "Dry run"

    Validate the samplesheet and parameters without launching tasks.

    ```bash
    nextflow run main.nf \
      --input samplesheet.csv \
      --outdir results \
      --start preprocessing \
      --dry_run true
    ```

!!! danger "Checkpoint paths are `csv/...`, not `results/<patient>/csv/...`"
    Checkpoint CSVs are written to **`csv/`** in your **launch directory** — one aggregated file per stage, covering all patients. They are **not** under `--outdir` and **not** per-patient. Run resume commands from the same working directory you launched the first run from. Details in [Restartability & Checkpoints](restartability_guide.md).

---

## Execution profiles

Profiles are defined in `nextflow.config` and combine with commas (`-profile slurm,singularity`).

| Profile | Purpose |
|---|---|
| `docker` | Run containers with Docker (typical local dev). |
| `singularity` | Run containers with Singularity/Apptainer (typical HPC). |
| `conda` | Resolve tool environments via Conda instead of containers. |
| `local` | Execute on the local machine, no scheduler. |
| `slurm` | Submit jobs to a SLURM cluster. See [Running on SLURM](slurm.md). |
| `ieo` | IEO cluster site profile. |
| `test` | Minimal test dataset + reduced resources. |
| `test_full` | Larger end-to-end test dataset. |
| `instantseg_test` | Test profile exercising the InstanSeg segmentation backend. |
| `cellsam_test` | Test profile exercising the CellSAM segmentation backend. |

```bash
# Local containers, quick smoke test
nextflow run main.nf -profile test,docker --outdir results

# HPC: SLURM scheduler with Singularity containers
nextflow run main.nf -profile slurm,singularity \
  --input samplesheet.csv --outdir results --start preprocessing
```

---

## Parameter presets

JSON presets in `params/` set sensible defaults for common scenarios. Load one with `-params-file` and override individual values on the command line.

| Preset | Use for |
|---|---|
| `params/full_pipeline.json` | All stages, starting from preprocessing. |
| `params/preprocessing_only.json` | Preprocessing alone. |
| `params/registration_only.json` | Registration from a `csv/preprocessed.csv` checkpoint. |
| `params/postprocessing_only.json` | Postprocessing from a `csv/registered.csv` checkpoint. |
| `params/test.json` | Minimal test run. |

```bash
nextflow run main.nf \
  -params-file params/full_pipeline.json \
  --input samplesheet.csv --outdir results \
  --seg_method instantseg          # override a preset value inline
```

!!! tip "Presets and the full surface"
    Presets only set a subset of parameters. The complete, documented parameter list lives in [Parameters](parameters.md) (backed by `nextflow_schema.json`).

---

## Site-specific configuration

For institution-specific settings (SLURM partition, account, resource caps), keep a config file under version control and layer it with `-c`. Values in a `-c` file override the bundled defaults.

```bash
nextflow run main.nf \
  -c my_site.config \
  -profile slurm,singularity \
  --input samplesheet.csv --outdir results --start preprocessing
```

A minimal `my_site.config`:

```groovy
params {
    slurm_partition = 'compute'
    slurm_account   = 'myproject'
    max_memory      = '500.GB'
    max_cpus        = 64
    max_time        = '120.h'
}
```

See [Running on SLURM](slurm.md) for the full set of cluster knobs.

---

## Resuming a failed run

Nextflow caches completed tasks in `work/`. Adding `-resume` re-uses that cache and only re-runs what changed or failed:

```bash
nextflow run main.nf -resume \
  --input samplesheet.csv \
  --outdir results \
  --start preprocessing \
  -profile docker
```

!!! note "`-resume` vs `--start` — different mechanisms"
    - `-resume` reuses the **Nextflow work cache** within the same run lineage.
    - `--start` **skips earlier stages entirely** by feeding a checkpoint CSV.

    They combine freely. The distinction is explained in depth in [Restartability & Checkpoints](restartability_guide.md).

---

## Related pages

- [Input Format](input_spec.md) — samplesheet columns per `--start`.
- [Parameters](parameters.md) — the complete parameter reference.
- [Running on SLURM](slurm.md) — HPC submission and resources.
- [Restartability & Checkpoints](restartability_guide.md) — checkpoint CSVs and resume patterns.
- [Troubleshooting](troubleshooting.md) — common failures and fixes.
