# Output Files

This page is the map of everything MIRAGE writes: where it goes, what each file
is, and the column schema for the tables you'll actually analyze.

There are **two output locations**:

<div class="grid cards" markdown>

-   :material-folder-multiple:{ .lg .middle } **Results — `--outdir`**

    ---

    Per-patient images, masks, tables, GeoJSON, pyramids, and QC. This is what
    you keep and analyze.

-   :material-file-restore:{ .lg .middle } **Checkpoints — `<outdir>/csv/`**

    ---

    Stage handoff CSVs, written to a single top-level `csv/` folder **under
    `--outdir`** — one aggregated file per stage, not per patient.

</div>

!!! warning "Checkpoint CSVs live in `<outdir>/csv/`, not the per-patient tree"
    Each stage aggregates **all patients** into one checkpoint file directly under
    `--outdir`: `<outdir>/csv/preprocessed.csv`, `<outdir>/csv/registered.csv`,
    `<outdir>/csv/postprocessed.csv`. Resume a later stage by pointing `--input`
    at one of them (e.g. `--input results/csv/registered.csv --start
    postprocessing` when `--outdir results`). They are **not** in the per-patient
    `<outdir>/<patient>/csv/` subtree — a common place people look by mistake.

## Results tree (`--outdir`)

Each patient gets a subdirectory; multiple patients are processed in parallel.

```text
results/                          # = --outdir
├── <patient_id>/
│   ├── converted/                # CONVERT_IMAGE — standardized OME-TIFF (DAPI → ch0)
│   ├── preprocessed/             # PREPROCESS — *_corrected.ome.tif
│   ├── registered/               # REGISTER — *_registered.ome.tiff
│   │   └── summary/              #   VALIS registration error summary CSVs
│   ├── feature_distances/        # ESTIMATE_FEATURE_DISTANCES — *.json, *.png  (if --enable_feature_error)
│   ├── segmentation/             # SEGMENT — *_nuclei_mask.tif, *_cell_mask.tif
│   ├── cell_properties/          # EXTRACT_CELL_PROPERTIES — morphology.csv, contours.json
│   │   └── nuclei/               #   nucleus morphology/contours  (if --quantify_compartments)
│   ├── quantification/           # MERGE_QUANT_CSVS — merged_quant.csv
│   ├── geojson/                  # EXPORT_GEOJSON — cells.geojson, cells_data.csv
│   ├── pyramid/                  # MERGE_AND_PYRAMID — *.ome.tiff
│   └── qc/
│       ├── preprocess/qc/        # *_*.png  illumination before/after
│       ├── registration/qc/      # *_QC_RGB.png/.tif, *_QC_RGB_fullres.tif
│       └── postprocessing/qc/    # segmentation overlay, intensity plots
├── csv/                          # checkpoint CSVs (all patients) — preprocessed/registered/postprocessed.csv
├── qc/                           # GENERATE_QC_REPORT — aggregated HTML report (all patients)
└── size_logs/                    # AGGREGATE_SIZE_LOGS — input_sizes.csv  (if --enable_trace)
```

!!! note "Intermediate process outputs"
    Processes without an explicit publish rule fall back to the default and
    publish under `<outdir>/<process_name_lowercased>/`. The directories above are
    the **curated, user-facing** outputs; those are the ones you'll work with.

## Checkpoint CSVs (`<outdir>/csv/`)

Written to a single `csv/` folder directly under `--outdir`, these are the
contract between stages — each a ready-made samplesheet for the next `--start`.
See [Restartability](restartability_guide.md).

=== "csv/preprocessed.csv"

    Written after **preprocessing**. Use as `--input` for `--start registration`.

    | Column | Description |
    |---|---|
    | `patient_id` | Patient identifier |
    | `preprocessed_image` | Absolute path to the illumination-corrected OME-TIFF |
    | `is_reference` | `true` for the registration reference panel |
    | `channels` | Pipe-separated channel names, in image order |

=== "csv/registered.csv"

    Written after **registration**. Use as `--input` for `--start postprocessing`.

    | Column | Description |
    |---|---|
    | `patient_id` | Patient identifier |
    | `registered_image` | Absolute path to the registered OME-TIFF |
    | `is_reference` | `true` for the reference panel |
    | `channels` | Pipe-separated channel names |

=== "csv/postprocessed.csv"

    Written after **postprocessing**. A manifest of the final per-patient
    artifacts, with columns:

    | Column | Points to |
    |---|---|
    | `patient_id` | Patient identifier |
    | `cell_csv` | `geojson/cells_data.csv` |
    | `cell_geojson` | `geojson/cells.geojson` |
    | `merged_csv` | `quantification/merged_quant.csv` |
    | `cell_mask` | `segmentation/*_cell_mask.tif` |
    | `pyramid` | `pyramid/*.ome.tiff` |

## Per-patient outputs in detail

### `converted/` — standardized images

| File | Format | Description |
|---|---|---|
| `*.ome.tif` | OME-TIFF | Bio-Formats input normalized to OME-TIFF, with **DAPI moved to channel 0** |

### `preprocessed/` — illumination-corrected images

| File | Format | Description |
|---|---|---|
| `*_corrected.ome.tif` | OME-TIFF | BaSiC flatfield/darkfield-corrected image. See [Preprocessing](preprocessing.md). |

### `registered/` — aligned images

| File | Format | Description |
|---|---|---|
| `*_registered.ome.tiff` | OME-TIFF | Panel warped into the reference coordinate space |
| `summary/*.csv` | CSV | VALIS registration error summary (D / rTRE per stage) — see [Error metrics](registration_errors.md) |

### `feature_distances/` — registration error *(optional)*

Written only with `--enable_feature_error true`.

| File | Format | Description |
|---|---|---|
| `*_feature_distances.json` | JSON | Before/after feature-match statistics and TRE |
| `*_distance_histogram.png` | PNG | Distance distribution before vs after registration |

Method details: [Feature Distance Estimation](estimate_feature_distances.md).

### `segmentation/` — cell & nuclei masks

Integer label masks produced by `SEGMENT` on the reference image (StarDist,
InstanSeg, or CellSAM — see [Segmentation](segmentation.md)).

| File | Format | Description |
|---|---|---|
| `*_cell_mask.tif` | TIFF (uint32) | Whole-cell instance labels; each unique non-zero value is one cell |
| `*_nuclei_mask.tif` | TIFF (uint32) | Nuclear instance labels |

### `cell_properties/` — morphology & contours

Computed once per patient and reused downstream.

| File | Format | Description |
|---|---|---|
| `morphology.csv` | CSV | Per-cell shape features (see table below) |
| `contours.json` | JSON | `label → [[x, y], …]` simplified polygon per cell |
| `nuclei/` | dir | Nucleus morphology + contours, present only with `--quantify_compartments` |

Morphology columns:

| Column | Unit | Description |
|---|---|---|
| `label` | — | Cell ID, matches the segmentation mask |
| `y`, `x` | px | Centroid coordinates |
| `area` | px² | Cell area |
| `perimeter` | px | Cell perimeter |
| `eccentricity` | — | 0 (circle) → 1 (line) |
| `solidity` | — | Area / convex area |
| `convex_area` | px² | Convex hull area |
| `axis_major_length`, `axis_minor_length` | px | Fitted-ellipse axes |

### `quantification/` — single-cell marker table

| File | Format | Description |
|---|---|---|
| `merged_quant.csv` | CSV | One row per cell; all markers + morphology joined |

Default (whole-cell) columns include `fov` (patient id), `cell_size`, `label`,
the morphology columns above, and one column per marker holding its **mean
intensity** within the cell. With `--quantify_compartments` you also get
`<MARKER>: Nucleus/Cytoplasm/Cell: Mean` columns; with
`--expanded_quantification`, additional `Median` and `Sum`. Full details and
worked examples: [Quantification](quantification.md).

### `geojson/` — QuPath-native export

A **single combined** GeoJSON per patient (no separate nuclei/whole-cell files):

| File | Format | Description |
|---|---|---|
| `cells.geojson` | GeoJSON | One Feature per cell: whole-cell polygon geometry + a QuPath measurement array (centroid µm, marker intensities, morphology µm) |
| `cells_data.csv` | CSV | The cell table with per-marker **z-scores** added |

With `--quantify_compartments`, each feature is a QuPath `cell` object carrying
its whole-cell outline as `geometry` **and** its nucleus outline as a top-level
`nucleusGeometry` member, plus per-compartment marker measurements. Without
compartments, features are `detection` objects with the whole-cell polygon only.

!!! info "No phenotyping step"
    MIRAGE does **not** assign cell types. The GeoJSON carries raw marker
    intensities (and the CSV adds z-scores) so you can gate and phenotype
    interactively downstream in QuPath and the [FlowPath ecosystem](flowpath.md).
    See [Visualization & Export](export.md).

A compartment-mode feature looks like:

```json
{
  "type": "Feature",
  "geometry":        { "type": "Polygon", "coordinates": [[[614.7, 512.3], "…"]] },
  "nucleusGeometry": { "type": "Polygon", "coordinates": [[[615.1, 512.9], "…"]] },
  "properties": {
    "objectType": "cell",
    "classification": { "name": "Cell" },
    "measurements": [
      { "name": "Centroid X µm",       "value": 199.8 },
      { "name": "CD8: Nucleus: Mean",  "value": 88.4 },
      { "name": "CD8: Cell: Mean",     "value": 312.1 },
      { "name": "Area µm²",            "value": 132.3 }
    ]
  }
}
```

### `pyramid/` — multi-resolution image

| File | Format | Description |
|---|---|---|
| `*.ome.tiff` | OME-TIFF | Tiled, pyramidal image combining all registered channels; opens in QuPath, napari, OMERO |

Controlled by `--pyramid_resolutions`, `--pyramid_scale`, `--tilex`/`--tiley`,
and `--compression`. See [Visualization & Export](export.md).

### `qc/` — per-patient quality control

| Subdirectory | Contents |
|---|---|
| `preprocess/qc/` | Per-channel illumination before/after PNGs |
| `registration/qc/` | RGB alignment overlays (`*_QC_RGB.png/.tif`, `*_QC_RGB_fullres.tif`) |
| `postprocessing/qc/` | Segmentation overlay and intensity-distribution plots |

## Run-level outputs

### `qc/` (top level) — aggregated report

Unless `--skip_final_qc_report`, `GENERATE_QC_REPORT` assembles every stage's QC
assets and the collated tool versions into a single HTML report under
`<outdir>/qc/`.

### `size_logs/` — resource tracing

With `--enable_trace` (default on), per-task input sizes are aggregated to
`<outdir>/size_logs/input_sizes.csv`. Nextflow also writes `trace.txt`,
`report.html`, and `timeline.html` to `--trace_dir` (default `.trace`).

## See also

<div class="grid cards" markdown>

- :material-sitemap:{ .lg .middle } **How it's produced** — [Pipeline Architecture](workflow.md)
- :material-restore:{ .lg .middle } **Resume from a checkpoint** — [Restartability](restartability_guide.md)
- :material-chart-scatter-plot:{ .lg .middle } **Quant column schema** — [Quantification](quantification.md)
- :material-shape-outline:{ .lg .middle } **Open in QuPath** — [Visualization & Export](export.md)

</div>
