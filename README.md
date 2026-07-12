# MIRAGE

[![Nextflow](https://img.shields.io/badge/nextflow%20DSL2-%E2%89%A525.04.0-23aa62.svg)](https://www.nextflow.io/)
[![run with docker](https://img.shields.io/badge/run%20with-docker-0db7ed?logo=docker)](https://www.docker.com/)
[![run with singularity](https://img.shields.io/badge/run%20with-singularity-1d355c.svg)](https://sylabs.io/docs/)

## Introduction

MIRAGE is a Nextflow DSL2 pipeline for whole slide image (WSI) processing. It supports multiplex fluorescence imaging workflows end-to-end: illumination correction, multi-modal registration, cell segmentation, single-cell marker quantification, and QuPath-compatible GeoJSON export for interactive gating via FlowPath. The pipeline is designed for HPC SLURM environments but also runs locally via Docker or Singularity.

## Pipeline Summary

1. **Image format conversion** — Input images are read from any Bio-Formats-compatible format and staged as OME-TIFF for downstream processing
2. **Illumination correction** (`PREPROCESS`) — Per-channel flatfield/darkfield correction via the BaSiC algorithm ([BaSiCPy](https://github.com/peng-lab/BaSiCPy)); produces corrected OME-TIFF per image
3. **Multi-modal image registration** (`REGISTER`) — Aligns all panels for a patient to a shared coordinate space using `valis` (graph-based whole-stack registration via [VALIS](https://github.com/MathOnco/valis))
4. **Cell segmentation** (`SEGMENT`) — Nuclear and cell segmentation via [StarDist](https://github.com/stardist/stardist); outputs nuclear and cell instance masks per patient
5. **Single-cell marker quantification** (`QUANTIFY`) — Extracts per-cell intensity statistics across all registered channels; outputs CSV tables
6. **QuPath GeoJSON export** (`EXPORT_GEOJSON`) — Exports all cells with raw marker intensities and morphological measurements in QuPath-native GeoJSON format for interactive gating via FlowPath
7. **Pyramidal OME-TIFF export** — Assembles all registered channels into a tiled, multi-resolution OME-TIFF for visualization (e.g., QuPath, napari)
8. **Quality control reporting** — QC overlays and metrics are generated at the preprocessing and registration steps

## Quick Start

### Prerequisites

1. Install [Nextflow](https://www.nextflow.io/docs/latest/getstarted.html) (`>=25.04.0`)
2. Install [Singularity/Apptainer](https://apptainer.org/) (recommended on HPC) or [Docker](https://docs.docker.com/get-docker/) (local/dev)
3. Clone the repository:

   ```bash
   git clone https://github.com/sceriff0/mirage.git
   cd mirage
   ```

### Full pipeline from raw images

Use `--start` to choose the entry point and `--stop` to terminate early at a given step. Both accept `preprocessing`, `registration`, or `postprocessing`. If `--stop` is omitted, the pipeline runs through to the end.

```bash
nextflow run . \
  --input samplesheet.csv \
  --outdir results \
  --start preprocessing \
  --stop registration \
  --registration_method valis \
  -profile slurm \
  -params-file params/full_pipeline.json
```

### Resume from registration checkpoint

```bash
nextflow run . \
  --input results/csv/preprocessed.csv \
  --start registration \
  --registration_method valis \
  --outdir results \
  -profile slurm \
  -resume
```

### Resume from postprocessing checkpoint

```bash
nextflow run . \
  --input results/csv/registered.csv \
  --start postprocessing \
  --outdir results \
  -profile slurm \
  -resume
```

### Dry-run (validation only)

```bash
nextflow run . \
  --input samplesheet.csv \
  --start preprocessing \
  --dry_run true
```

## Documentation

- [Usage](docs/usage.md) — samplesheet format, parameters, profiles
- [Outputs](docs/usage.md#outputs) — output directory layout and file descriptions
- [Quick start walkthrough](docs/usage.md#quick-start-synthetic-data) — end-to-end run on the bundled test data
- [Troubleshooting](docs/usage.md#troubleshooting--faq) — common failures and remediation
- [Parameters](docs/parameters.md) — full parameter reference
- [Citation](docs/citation.md) — how to cite MIRAGE and its dependencies

Full documentation (MkDocs):

```bash
pip install -r docs/requirements.txt
mkdocs serve
```

## Credits

MIRAGE was developed by the MIRAGE team.

## Citations

If you use MIRAGE in your research, please cite the relevant tools listed in [CITATIONS.md](CITATIONS.md).

Core dependencies include:

- **VALIS** — Gatenbee et al. (2023), *Nature Communications* — [https://doi.org/10.1038/s41467-023-40218-9](https://doi.org/10.1038/s41467-023-40218-9)
- **StarDist** — Schmidt et al. (2018), *MICCAI* — [https://doi.org/10.1007/978-3-030-00934-2_30](https://doi.org/10.1007/978-3-030-00934-2_30)
- **BaSiCPy** — Peng et al. (2017), *Nature Communications* — [https://doi.org/10.1038/ncomms14836](https://doi.org/10.1038/ncomms14836)
- **Nextflow** — Di Tommaso et al. (2017), *Nature Biotechnology* — [https://doi.org/10.1038/nbt.3820](https://doi.org/10.1038/nbt.3820)
