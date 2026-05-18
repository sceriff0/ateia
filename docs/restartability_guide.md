# Restartability

MIRAGE is checkpoint-driven. Each major stage writes a CSV that can be used as input to the next stage.

## Valid Step Entrypoints

- `preprocessing`
- `registration`
- `postprocessing`

## Restart Patterns

## Full pipeline from raw/preprocessing input

```bash
nextflow run main.nf \
  --input input.csv \
  --start preprocessing \
  --outdir results
```

## Resume at registration

```bash
nextflow run main.nf \
  --input results/P001/csv/preprocessed.csv \
  --start registration \
  --outdir results
```

## Resume at postprocessing

```bash
nextflow run main.nf \
  --input results/P001/csv/registered.csv \
  --start postprocessing \
  --outdir results
```
