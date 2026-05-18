# MIRAGE Documentation

MIRAGE is a Nextflow DSL2 pipeline for multiplex microscopy processing, designed for whole-slide image (WSI) workflows on HPC and local environments. It runs end-to-end in three stages:

1. **Preprocessing** — Bio-Formats conversion and BaSiC illumination correction
2. **Registration** — VALIS whole-slide image registration across panels
3. **Postprocessing** — StarDist segmentation, single-cell marker quantification, QuPath-compatible GeoJSON export, and pyramidal OME-TIFF assembly

This site is the canonical user-facing documentation. The repository, issues, and source of truth for parameter defaults live at <https://github.com/sceriff0/mirage>.

## New to MIRAGE?

Read in this order:

1. [Installation](installation.md) — Nextflow, container runtime, repo clone
2. [Walkthrough](walkthrough.md) — end-to-end run on the bundled synthetic test data
3. [Getting Started](getting_started.md) — quick reference for the most common invocations
4. [Input Format](input_spec.md) — CSV samplesheet schema for each entry point
5. [Workflow](workflow.md) — step routing and channel flow at a glance

## Canonical Runtime Surface

The pipeline is driven entirely by a small set of Nextflow params:

| Flag | Purpose | Accepted values |
|---|---|---|
| `--input` | Path to the samplesheet CSV. Required. | absolute or relative path |
| `--outdir` | Output directory. Required. | path; will be created |
| `--start` | Entry point. | `preprocessing`, `registration`, `postprocessing` |
| `--stop` | Exit point. Omit to run to the end. | `preprocessing`, `registration`, `postprocessing` |
| `--dry_run` | Validate inputs without launching tasks. | `true` / `false` |
| `-profile` | Comma-separated profile combination from `nextflow.config`. | `test,docker`, `slurm,singularity`, … |
| `-params-file` | JSON parameter preset. | `params/test.json`, `params/full_pipeline.json`, … |

Use `--start X --stop X` to run a single stage. Use `--start X` alone to run from `X` to the end. See [Parameters](parameters.md) for the full surface including resource, registration, and segmentation knobs.

## Source of Truth

- Pipeline entrypoint: [`main.nf`](https://github.com/sceriff0/mirage/blob/main/main.nf)
- Parameter defaults: [`nextflow.config`](https://github.com/sceriff0/mirage/blob/main/nextflow.config)
- Parameter schema: [`nextflow_schema.json`](https://github.com/sceriff0/mirage/blob/main/nextflow_schema.json)

When the docs and the schema disagree, the schema wins — please open an issue so we can correct the docs.

## Citing MIRAGE

See the [Citation](citation.md) page for the recommended citation text and the underlying tool references. Always pin to a tagged release or commit SHA when citing a run.
