# Getting Started

## Minimal Prerequisites

- Nextflow (DSL2-capable)
- Container runtime (`singularity` or `docker`)
- CSV input with required columns for your selected `--start`

## Quick Run: Full Pipeline

```bash
nextflow run main.nf \
  --input input.csv \
  --outdir results \
  --start preprocessing \
  --registration_method valis \
  -profile slurm
```

## Quick Run: Registration from Checkpoint

```bash
nextflow run main.nf \
  --input results/P001/csv/preprocessed.csv \
  --start registration \
  --registration_method valis \
  --outdir results \
  -profile slurm
```

## Archiving Results

To archive results, use standard file tools (e.g., `rsync`) to copy `--outdir` to your archive location.

## Dry Validation Only

```bash
nextflow run main.nf \
  --input input.csv \
  --start preprocessing \
  --dry_run true
```

## First Checks After Launch

1. Verify `results/{patient_id}/csv/` checkpoint CSVs are created.
2. Confirm your chosen registration method outputs in `results/<patient_id>/registered/`.
3. Confirm postprocessing outputs in `results/<patient_id>/`.

