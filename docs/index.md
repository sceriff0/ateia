---
hide:
  - navigation
  - toc
---

<div class="mirage-hero" markdown>

# MIRAGE

**Multiplex Imaging Registration, Analysis, & GeoJSON Export**

A Nextflow DSL2 pipeline that takes raw whole-slide microscopy from many panels
and turns it into aligned images, segmented cells, single-cell marker tables, and
QuPath-ready GeoJSON — reproducibly, on your laptop or an HPC cluster.

<div class="mirage-badges" markdown>
[:material-rocket-launch: Install](installation.md){ .md-button .md-button--primary }
[:material-walk: How to use](usage.md){ .md-button }
[:fontawesome-brands-github: Source](https://github.com/sceriff0/mirage){ .md-button }
</div>

</div>

[![Nextflow](https://img.shields.io/badge/nextflow%20DSL2-%E2%89%A525.04.0-23aa62.svg)](https://www.nextflow.io/)
[![run with docker](https://img.shields.io/badge/run%20with-docker-0db7ed?logo=docker)](https://www.docker.com/)
[![run with singularity](https://img.shields.io/badge/run%20with-singularity-1d355c.svg)](https://sylabs.io/docs/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/sceriff0/mirage/blob/main/LICENSE)

## What MIRAGE does

MIRAGE is built for **multiplex fluorescence imaging**, where each tissue sample
is imaged across several panels of markers. It stitches those panels into one
coordinate space, finds every cell, measures every marker, and hands you
analysis-ready tables and QuPath-native overlays.

```mermaid
flowchart LR
    A[Raw WSI panels<br/>ND2 · CZI · LIF · TIFF] --> B
    subgraph PRE[Preprocessing]
      B[Convert to OME-TIFF] --> C[BaSiC illumination<br/>correction]
    end
    C --> D
    subgraph REG[Registration]
      D[VALIS whole-slide<br/>alignment]
    end
    D --> E
    subgraph SEG[Segmentation]
      E[Segment cells<br/>+ extract properties]
    end
    E --> F
    subgraph POST[Postprocessing]
      F[Quantify markers<br/>per cell] --> G[GeoJSON + pyramid<br/>OME-TIFF]
    end
    G --> H[(QuPath · napari ·<br/>FlowPath)]
```

The four stages — **preprocessing → registration → segmentation → postprocessing** — run in that
order and are independently restartable, so you can re-run just the part you're
tuning. By default MIRAGE **stops at quantified cells** — the exported GeoJSON carries
raw marker intensities, and you gate/phenotype downstream in QuPath or the
[FlowPath](https://flowpath.readthedocs.io/) ecosystem. Optional built-in
[phenotyping](parameters.md#phenotyping-panel-agnostic) (panel-driven, conformal-risk
controlled) can assign cell types directly into the GeoJSON when a panel is supplied.

## Highlights

- **Any Bio-Formats input** — reads ND2, CZI, LIF, NDPI, TIFF, HDF5 and more,
  normalizes to OME-TIFF, and guarantees DAPI lands on channel 0.
- **VALIS registration** — deep-feature rigid + non-rigid alignment of every panel
  to a shared reference, with quantitative error metrics.
- **Three segmentation backends** — swap between **StarDist**, **InstanSeg**, and
  **CellSAM** with a single `--seg_method` flag.
- **Single-cell quantification** — per-cell marker intensities, optional
  nucleus / cytoplasm / cell compartments, and morphology.
- **QuPath-native export** — GeoJSON with raw intensities and z-scores, plus a
  pyramidal OME-TIFF for interactive viewing and gating.
- **Restartable & HPC-ready** — checkpoint CSVs between stages, SLURM/Singularity
  profiles, and resource-aware retries.

## A 30-second taste

```bash
# 1. Clone and generate the bundled synthetic test data
git clone https://github.com/sceriff0/mirage.git && cd mirage
python tests/testdata/generate_complete_testdata.py

# 2. Run the whole pipeline on the test data (CPU, ~15 min)
nextflow run . -profile test,docker --outdir results
```

That's the full preprocessing → registration → segmentation → postprocessing run. [Usage](usage.md)
tours every file it produces.

## Get started

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } **Install**

    ---

    Nextflow, a container runtime, and a clone of the repo. You're five minutes
    from your first run.

    [:octicons-arrow-right-24: Installation](installation.md)

-   :material-walk:{ .lg .middle } **Run it**

    ---

    The four-stage model, the samplesheet, resuming from checkpoints, and what
    lands in `--outdir`.

    [:octicons-arrow-right-24: Usage](usage.md)

-   :material-tune:{ .lg .middle } **Tune it**

    ---

    Every flag, grouped by stage, with defaults and guidance.

    [:octicons-arrow-right-24: Parameters](parameters.md)

</div>

!!! note "Source of truth"
    When the docs and the code disagree, the code wins —
    [`nextflow.config`](https://github.com/sceriff0/mirage/blob/main/nextflow.config)
    holds parameter defaults and
    [`nextflow_schema.json`](https://github.com/sceriff0/mirage/blob/main/nextflow_schema.json)
    the schema. Please [open an issue](https://github.com/sceriff0/mirage/issues)
    if you spot a mismatch. Found MIRAGE useful? See [Citation](citation.md).
