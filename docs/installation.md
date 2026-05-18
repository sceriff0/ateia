# Installation

## Requirements

- Java 11+
- Nextflow
- One container backend:
  - Singularity/Apptainer (recommended on HPC)
  - Docker (local/dev)

## Clone and Enter Repository

```bash
git clone https://github.com/sceriff0/mirage.git
cd mirage
```

## Verify Nextflow

```bash
nextflow -version
```

## ReadTheDocs/MkDocs Dependencies

Local docs build dependencies are in `docs/requirements.txt`:

```bash
pip install -r docs/requirements.txt
mkdocs serve
```

## Execution Profiles

Defined in `nextflow.config` under the `profiles { ... }` block. Combine profiles with a comma (e.g. `-profile test,docker`):

- `test` — minimal synthetic dataset for CI and smoke tests
- `test_full` — larger synthetic dataset with realistic params
- `docker` — run processes in Docker containers (local/dev)
- `singularity` — run processes in Singularity/Apptainer containers (recommended on HPC)
- `conda` — run with Conda-managed environments (no containers)
- `slurm` — submit each process as a SLURM job
- `local` — local executor with conservative caps (4 CPUs / 16 GB RAM)
- `ieo` — site-specific config for the IEO HPC cluster (gitignored; copy from `site.config.template`)

## Container Images

Containers are defined under `params.container` in `nextflow.config` and process-specific settings in `conf/modules.config`.

