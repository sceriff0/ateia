# Output

All outputs are written under `--outdir` (default `./results`).

## Directory Structure

```
results/
  csv/
    preprocessed.csv
    registered.csv
    postprocessed.csv
  <patient_id>/
    preprocessed/
    registered/
    qc/
      preprocess/
      registration/
    segmentation/
    quantification/
    phenotyping/
    pyramid/
  pipeline_info/      (if enable_trace = true)
```

Multiple patients are processed in parallel. Each patient gets its own subdirectory under `--outdir`.

---

## Checkpoint CSVs (`results/csv/`)

These files are generated at the end of each major step and serve as `--input` for downstream checkpoint runs.

### `preprocessed.csv`

Generated after the `preprocessing` step. Use as `--input` when running `--step registration`.

Columns:

| Column | Description |
|---|---|
| `patient_id` | Patient identifier |
| `preprocessed_image` | Absolute path to the illumination-corrected OME-TIFF |
| `is_reference` | Whether this image is the registration reference |
| `channels` | Pipe-separated channel names in image order |

### `registered.csv`

Generated after the `registration` step. Use as `--input` when running `--step postprocessing`.

Columns:

| Column | Description |
|---|---|
| `patient_id` | Patient identifier |
| `registered_image` | Absolute path to the registered OME-TIFF |
| `is_reference` | Whether this image is the registration reference |
| `channels` | Pipe-separated channel names in image order |

### `postprocessed.csv`

Manifest of postprocessing outputs. Contains paths to quantification CSVs, phenotyping CSVs, and GeoJSON files per patient.

---

## Per-Patient Outputs

### `<patient_id>/preprocessed/`

Contains illumination-corrected images produced by the BaSiC algorithm.

| File | Format | Description |
|---|---|---|
| `*_corrected.ome.tif` | OME-TIFF | Flatfield- and darkfield-corrected image; DAPI is guaranteed to be channel 0 |

### `<patient_id>/registered/`

Contains spatially aligned images produced by the selected registration method.

| File | Format | Description |
|---|---|---|
| `*_registered.ome.tiff` | OME-TIFF | Registered image aligned to the reference panel coordinate space |

### `<patient_id>/qc/preprocess/`

Quality control thumbnails for visual inspection of illumination correction.

| File | Format | Description |
|---|---|---|
| `*_qc_*.png` | PNG | Per-channel thumbnail overlays showing before/after correction |

### `<patient_id>/qc/registration/`

Quality control overlays for visual inspection of registration accuracy.

| File | Format | Description |
|---|---|---|
| `*_overlay.png` | PNG | RGB composite showing channel alignment between panels |
| `*_error.csv` | CSV | Feature-based registration error metrics (if `enable_feature_error = true`) |

### `<patient_id>/segmentation/`

Cell segmentation outputs produced by StarDist on the reference image.

| File | Format | Description |
|---|---|---|
| `*_mask.tif` | TIFF | Integer label image; each unique non-zero value is one cell instance |
| `*_cells.geojson` | GeoJSON | Polygon contours for each detected cell; compatible with QuPath and napari |

The GeoJSON follows the standard Feature Collection format. Each feature is a cell polygon with properties including a unique cell ID.

### `<patient_id>/quantification/`

Per-cell marker intensity statistics extracted from registered images.

| File | Format | Description |
|---|---|---|
| `*_quant.csv` | CSV | Per-cell intensity table; one row per cell, columns are marker channel names plus spatial metadata |
| `*_merged.csv` | CSV | Merged quantification across all panels for a patient; used as input to phenotyping |

Key columns in quantification CSVs:

| Column | Description |
|---|---|
| `cell_id` | Unique integer cell identifier matching the segmentation mask |
| `<MARKER>` | Mean intensity of the named channel within the cell |
| `area` | Cell area in pixels |
| `centroid_x`, `centroid_y` | Cell centroid coordinates in the registered image space |

### `<patient_id>/phenotyping/`

Cell type annotations produced by rule-based phenotyping.

| File | Format | Description |
|---|---|---|
| `*_phenotyped.csv` | CSV | Merged quantification table with an added `phenotype` column; one row per cell |
| `*_phenotyped.geojson` | GeoJSON | Cell polygon contours with phenotype label in the feature properties; compatible with QuPath |

The GeoJSON `properties` object for each cell includes at minimum:

```json
{
  "cell_id": 1234,
  "phenotype": "CD8_T_cell",
  "area": 85,
  "centroid_x": 4200.5,
  "centroid_y": 3100.2
}
```

### `<patient_id>/pyramid/`

Multi-resolution pyramidal OME-TIFF for interactive visualization.

| File | Format | Description |
|---|---|---|
| `*_pyramid.ome.tiff` | OME-TIFF | Tiled, multi-resolution image combining all registered channels; suitable for QuPath, napari, OMERO |

Resolution levels are controlled by `--pyramid_resolutions` and `--pyramid_scale`. Tile size is set by `--tilex` / `--tiley`. Compression codec is set by `--compression` (default `zstd`).

---

## Pixie Outputs (Optional)

Produced only when `--pixie_enabled true`.

| Path | Format | Description |
|---|---|---|
| `<patient_id>/pixie/pixel_clusters/` | CSV + PNG | Pixel SOM and meta-cluster assignments |
| `<patient_id>/pixie/cell_clusters/` | CSV + PNG | Cell-level cluster assignments derived from pixel clusters |

---

## Trace and Pipeline Info (`pipeline_info/`)

Generated when `--enable_trace true` (default). Written to the directory specified by `--trace_dir` (default `.trace`).

| File | Format | Description |
|---|---|---|
| `trace.txt` | TSV | Per-task resource usage (CPU, memory, walltime, I/O) |
| `report.html` | HTML | Interactive summary report of all tasks |
| `timeline.html` | HTML | Gantt-style execution timeline |
