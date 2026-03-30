# Workflow

## Pipeline Stages

## 1. Preprocessing

- Convert/normalize source images
- Correct illumination artifacts
- Split channels
- Emit checkpoint: `results/{patient_id}/csv/preprocessed.csv`

## 2. Registration

- Group by patient
- Select reference image
- Register moving images to reference using selected method
- Emit checkpoint: `results/{patient_id}/csv/registered.csv`

## 3. Postprocessing

- Segmentation
- Quantification
- Phenotyping
- Merge channels and produce pyramidal OME-TIFF
- Emit checkpoint: `results/{patient_id}/csv/postprocessed.csv`

## Workflow Control

`main.nf` orchestrates step-specific entrypoints:

- `preprocessing`: runs all downstream stages
- `registration`: starts from preprocessed checkpoint
- `postprocessing`: starts from registered checkpoint

## Tracing

If `enable_trace=true`, Nextflow trace/report/timeline files are written to `trace_dir`.

