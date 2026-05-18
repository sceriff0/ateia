# End-to-End Walkthrough

This page takes a brand-new user from a clean clone to a populated `results/` tree using the bundled synthetic test data. Total wall time on a modern laptop: **roughly 10–20 minutes**, almost entirely spent in segmentation and quantification on CPU. No GPU and no HPC are required.

## 0. Prerequisites

You will need:

- **Java 11+** (`java -version`)
- **Nextflow `>=25.04.0`** (`nextflow -version`; see [Installation](installation.md) for upgrade instructions)
- **Docker** (recommended for this walkthrough; Singularity also works — substitute `singularity` for `docker` in the `-profile` flag)
- **Python 3.10+** with `numpy`, `tifffile`, `pandas` installed (used to synthesise the test data)
- **~10 GB free disk space** for containers and intermediate outputs

If you already have Nextflow but it's older than 25.04.0, run `nextflow self-update`.

## 1. Clone the repository and generate test data

```bash
git clone https://github.com/sceriff0/mirage.git
cd mirage
python tests/testdata/generate_complete_testdata.py
```

The generator writes synthetic multi-channel OME-TIFFs and the matching CSV samplesheets into `tests/testdata/`. It uses a fixed seed (42) so two users running the same script get bit-identical fixtures, which is what makes the test profile reproducible across machines.

Verify the fixtures exist:

```bash
ls tests/testdata/test_input.csv tests/testdata/*.ome.tif
```

## 2. Stub run — a 30-second sanity check

A **stub run** is a Nextflow feature that executes each process's `stub:` block (a placeholder that creates empty output files with the right names) instead of the real `script:` block. It validates that channels connect correctly and that every process has its expected outputs declared, without ever running the real tools.

```bash
nextflow run . -profile test,docker -stub --outdir results_stub
```

What to look for in the output:

- A short DAG executing in seconds, not minutes
- All processes show `cached` or `completed` (no `FAILED`)
- `results_stub/` is created and contains the expected directory structure (placeholders for every output file)

If the stub run fails, your real run will fail in the same place — fix it before moving on.

## 3. Real run on synthetic test data

```bash
nextflow run . -profile test,docker --outdir results
```

The `test` profile (defined in `conf/test.config`) sets:

- `--input` to `tests/testdata/test_input.csv` (no need to pass `--input` manually)
- `max_cpus = 2`, `max_memory = 6.GB`, `max_time = 1.h`
- `seg_gpu = false` so StarDist runs on CPU
- Reduced `preproc_tile_size`, `feature_n_features`, and `memory_mode = 'low'` to keep the run small

The full three-stage pipeline (preprocessing → registration → postprocessing) will execute. Watch the per-process progress in the Nextflow console. Expected timing on a 4-core laptop is on the order of 10–15 minutes; the bulk of the time is in `SEGMENT` and `QUANTIFY`.

## 4. Tour of `results/`

After a successful run you'll have a per-patient subtree. For the test data the patient ID is `P001`:

```
results/
└── P001/
    ├── csv/
    │   ├── preprocessed.csv      # checkpoint for --start registration
    │   ├── registered.csv        # checkpoint for --start postprocessing
    │   └── postprocessed.csv     # manifest of postprocessing outputs
    ├── preprocessed/             # illumination-corrected OME-TIFFs
    ├── registered/               # VALIS-registered OME-TIFFs
    ├── qc/
    │   ├── preprocess/           # per-channel before/after PNGs
    │   └── registration/         # alignment overlays + (optionally) TRE CSVs
    ├── segmentation/             # *_mask.tif + *_cells.geojson
    ├── quantification/           # per-cell intensity tables
    ├── phenotyping/              # *_phenotyped.csv + *_phenotyped.geojson
    └── pyramid/                  # *_pyramid.ome.tiff for QuPath/napari
```

Files worth opening:

- `results/P001/qc/preprocess/*.png` — visual confirmation that illumination correction did something sensible.
- `results/P001/qc/registration/*_overlay.png` — RGB composite where each channel comes from a different panel; cell structures should align cleanly.
- `results/P001/segmentation/*_cells.geojson` — drop into [QuPath](https://qupath.github.io/) ("File → Import objects from file") to see segmented cells overlaid on the pyramid image.
- `results/P001/quantification/*_quant.csv` — one row per cell with mean intensity per marker, area, centroid; the canonical analysis table.
- `results/P001/pyramid/*_pyramid.ome.tiff` — the multi-resolution image for visualisation.

See [Outputs](outputs.md) for the full column-level schema of every CSV and the GeoJSON property layout.

## 5. Re-running a single stage

Each stage emits a checkpoint CSV in `results/P001/csv/` that can be fed back into a later stage. To re-run only postprocessing (e.g., after tuning segmentation params) without redoing preprocessing or registration:

```bash
nextflow run . \
  --input results/P001/csv/registered.csv \
  --outdir results \
  --start postprocessing \
  -profile test,docker \
  -resume
```

The combination of `--start postprocessing` and `-resume` makes Nextflow reuse all cached upstream tasks. See [Restartability](restartability_guide.md) for the full pattern with all three entry points.

## 6. Next steps

- **Tune parameters** — [Parameters](parameters.md) lists every flag with its default and meaning. The most impactful knobs on real data are `--memory_mode`, `--feature_n_features`, `--seg_pmin/--seg_pmax`, and `--seg_expand_distance`.
- **Run on real data** — swap the samplesheet for your own (schema in [Input Format](input_spec.md)) and use `params/full_pipeline.json` as a starting parameter preset.
- **Move to HPC** — `-profile slurm,singularity` instead of `test,docker`. See [SLURM](slurm.md) for partition/QoS configuration.
- **Hit a snag?** — see [Troubleshooting](troubleshooting.md) before filing an issue.
