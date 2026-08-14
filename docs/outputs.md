# Inputs & outputs

<p class="standfirst">Everything the pipeline reads and everything it writes: the samplesheet contract
per entry point, the checkpoint CSVs that make each step resumable, the complete published tree, and
the measurement-key grammar that the QuPath/FlowPath side depends on.</p>

!!! abstract "Canonical sources"
    - **Samplesheet columns per step** — `lib/ParamUtils.groovy` (`STEPS`)
    - **Checkpoint CSV headers** — `lib/Checkpoint.groovy` (`STEPS`)
    - **Published paths** — `lib/Layout.groovy` + the `publishDir` blocks in `conf/modules.config`
    - **Measurement keys** — `bin/utils/measurements.py`

    `lib/Layout.groovy` is the single owner of published-path and checkpoint-CSV
    layout. `tests/test_layout.py` forbids hand-written paths elsewhere, and
    `tests/checkpoint_manifest.nf.test` asserts that **every path recorded in
    every checkpoint CSV exists on disk and does not lie inside the work
    directory**.

---

## Inputs

### `--input` — the samplesheet { #the-samplesheet }

One CSV. Each row is one image. The required columns depend on `--start`,
because each step's entry point is the previous step's checkpoint.

| `--start` | Required columns | Image column read |
|---|---|---|
| `preprocessing` *(default)* | `patient_id`, `path_to_file`, `is_reference`, `channels` | `path_to_file` |
| `registration` | `patient_id`, `preprocessed_image`, `is_reference`, `channels` | `preprocessed_image` |
| `segmentation` | `patient_id`, `registered_image`, `is_reference`, `channels` | `registered_image` |
| `postprocessing` | `patient_id`, `registered_image`, `is_reference`, `channels`, `cell_mask`, `nuclei_mask` | `registered_image` |

Because the checkpoint CSV a step writes carries exactly the columns the next
step requires, resuming is a matter of pointing `--input` at the right
checkpoint — see [Checkpoints](#checkpoints).

#### Column semantics

| Column | Type | Meaning |
|---|---|---|
| `patient_id` | string | Grouping key. All rows sharing it are registered into one coordinate space and produce one output tree. |
| `path_to_file` | path | The image. Any Bio-Formats-readable format (ND2, CZI, LIF, NDPI, TIFF, HDF5, OME-TIFF). |
| `is_reference` | `true` / `false` | Exactly **one** `true` per patient — the registration reference and the slide that gets segmented. With `--allow_auto_reference true`, a patient with no `true` promotes its first image instead of failing. |
| `channels` | `\|`-separated | Marker names in channel order, e.g. `DAPI\|PanCK\|CD45`. **Declared metadata, never parsed from the filename.** The count must match the image's channel count. |

!!! warning "The nuclear channel is resolved by name, not position"
    `nuclear_markers` (default `['DAPI', 'CELLTOX']`) is an **ordered preference
    list**. `CONVERT_IMAGE` finds the first of those markers present in the
    row's `channels` and moves it to index 0. That one channel then drives
    **both** cell segmentation and the registration fiducial. If no listed
    marker is present the run fails fast at validation — single-channel images
    excepted. Matching is case-insensitive **substring**, so `DAPI_nuclear`
    counts as nuclear.

Validation runs before any task is submitted (and is exercised by `--dry_run`):
per-row format, per-patient reference counts, channel-count agreement, and file
existence.

### Other inputs

| Input | Parameter | Notes |
|---|---|---|
| Panel spec | `--panel_spec` | `panel.yaml`. Optional; enables phenotyping. |
| Compiled panel model | `--panel_model` | A frozen `model_config.json` from a previous `COMPILE_PANEL`. Use instead of `--panel_spec` to reuse a calibration. |
| StarDist model dir | `--segmentation_model_dir` | Required when `--seg_method stardist` — no model ships with the repo. |
| InstanSeg cache | `--instanseg_model_dir` | Writable BioImage.IO cache; exported as `INSTANSEG_BIOIMAGEIO_PATH`. |
| CellSAM weights | `--cellsam_model_path` | Pre-downloaded weights. If unset, weights auto-download and need `DEEPCELL_ACCESS_TOKEN` in the launch environment. |
| Prior run | `--prior_outdir` | `add_cycle` only — the `--outdir` of a completed run. |

---

## Checkpoints

Each step ends by writing one CSV under `<outdir>/csv/`. These are the
resume points, and their headers are a published contract
(`lib/Checkpoint.groovy`).

| File | Written after | Columns |
|---|---|---|
| `csv/preprocessed.csv` | preprocessing | `patient_id`, `preprocessed_image`, `is_reference`, `channels` |
| `csv/registered.csv` | registration | `patient_id`, `registered_image`, `is_reference`, `channels` |
| `csv/segmented.csv` | segmentation | `patient_id`, `registered_image`, `is_reference`, `channels`, `cell_mask`, `nuclei_mask`, `contours`, `nucleus_contours` |
| `csv/postprocessed.csv` | postprocessing | `patient_id`, `cell_csv`, `cell_geojson`, `merged_csv`, `cell_mask`, `pyramid` |

```bash
# run preprocessing only …
nextflow run . --input samplesheet.csv --outdir results --stop preprocessing

# … then pick up from its checkpoint later
nextflow run . --input results/csv/preprocessed.csv --outdir results --start registration
```

!!! note "The schema is fixed across parameter settings"
    `nucleus_contours` is empty when `--quantify_compartments false` (the
    extractor does not run), but the **column is still present**. Readers test
    for an empty value, never for a missing column — one header serves every
    run.

`mode=add_cycle` reads two of these out of `--prior_outdir`: `csv/registered.csv`
(for the frozen reference) and `csv/postprocessed.csv` (for the mask pyramid and
the merged quantification table).

---

## Outputs

Two locations: per-patient results under `<outdir>/<patient_id>/`, and run-level
aggregates at the `<outdir>` root.

### The published tree

The per-patient leaf directories are the closed vocabulary declared by
`Layout.PUBLISHED_KINDS` — a process publishing anywhere else under
`<outdir>/<patient_id>/` fails `tests/test_layout.py`. There are two per-patient
exceptions: `spatialdata/`, where `EXPORT_SPATIALDATA` publishes at the patient
root with its own `pattern:` supplying the directory name (no `Layout` kind for
it), and `qc/`, which `tests/test_layout.py` explicitly excludes from the check
because no checkpoint CSV names it — it is likewise absent from
`Layout.PUBLISHED_KINDS`.

```text
results/                              # = --outdir
├── <patient_id>/
│   ├── converted/                    # <name>.ome.tif        — CONVERT_IMAGE (nuclear → ch0)
│   ├── preprocessed/                 # *_corrected.ome.tif   — PREPROCESS (BaSiC)
│   │                                 #   absent when --skip_preprocessing; csv/preprocessed.csv
│   │                                 #   then points at converted/ instead
│   ├── registered/
│   │   ├── registered_slides/        # *_registered.ome.tiff — REGISTER (VALIS)
│   │   ├── registered/               # *_registered.ome.tiff — TILED_STITCH
│   │   ├── manifest/                 # *_manifest.json       — tiled backend
│   │   └── summary/                  # *.csv                 — VALIS error summary
│   ├── segmentation/                 # *_cell_mask.tif, *_nuclei_mask.tif  — SEGMENT
│   ├── cell_properties/
│   │   ├── morphology.csv            # EXTRACT_CELL_PROPERTIES
│   │   ├── contours.json
│   │   └── nuclei/                   # morphology.csv, contours.json — EXTRACT_NUCLEI_PROPERTIES
│   ├── split_channels/               # <MARKER>.tiff, one per marker — SPLIT_CHANNELS
│   │   └── prior/                    # add_cycle only: prior pyramid re-split
│   ├── quantify/                     # <id>_quant.csv, per-marker, pre-merge — QUANTIFY
│   ├── quantification/               # merged_quant.csv      — MERGE_QUANT_CSVS
│   ├── phenotyping/                  # phenotypes.csv, constraint_audit.csv,
│   │                                 #   phenotype_qc.json   — PHENOTYPE (if a panel is set)
│   ├── geojson/
│   │   └── export/                   # cells.geojson, cells_wholecell.geojson,
│   │                                 #   cells_data.csv, panel_model.json — EXPORT_GEOJSON
│   ├── pyramid/                      # pyramid.ome.tiff      — MERGE_AND_PYRAMID
│   ├── spatialdata/                  # <patient_id>.zarr     — EXPORT_SPATIALDATA
│   └── qc/
│       ├── preprocess/
│       │   └── qc/                   # *.png — GENERATE_PREPROCESS_QC
│       ├── registration/             # *_seg_qc.json — WARP_SEG_QC
│       │   │                         # *_tre.json    — TILED_SOLVE (tiled backend)
│       │   ├── qc/                   # *_QC_RGB.{png,tif}, *_QC_RGB_fullres.tif
│       │   │                         #   — GENERATE_REGISTRATION_QC
│       │   └── geojson/              # *.geojson — SEG_QC_GEOJSON (reg_qc=2)
│       └── postprocessing/
│           └── qc/                   # *.png — GENERATE_POSTPROCESSING_QC
├── phenotyping/                      # model_config.json, spec_report.html — COMPILE_PANEL
│                                     #   (run-level: one panel shared across patients)
├── csv/                              # checkpoint CSVs, all patients
├── size_logs/                        # input_sizes.csv, versions.yml — AGGREGATE_SIZE_LOGS
└── qc/                               # mirage_qc_report_<timestamp>.html — GENERATE_QC_REPORT
                                      # mirage_resource_report.html — main.nf's
                                      #   workflow.onComplete handler, not a process
```

!!! info "Why `geojson/export/`, `registered/registered_slides/` and the repeated `qc/`"
    A process that writes into a named subdirectory of its task directory and
    publishes with a `pattern:` naming that subdirectory gets the subdirectory
    carried into the published path. `Layout.publishedPath` reproduces that
    faithfully rather than flattening it — a checkpoint row that named
    `<pid>/geojson/cells.geojson` instead of `<pid>/geojson/export/cells.geojson`
    pointed at a file that does not exist for two releases.

    The same rule is why the three QC renderers land under a *second* `qc/`:
    each declares `path("qc/*.png")` and publishes with `pattern: "qc/*.png"`,
    so the real path is `<pid>/qc/preprocess/qc/*.png`, not
    `<pid>/qc/preprocess/*.png`. The tree above used to show the flattened form.
    `WARP_SEG_QC` and `SEG_QC_GEOJSON` declare flat patterns and are unaffected.

### Trace outputs

Written to `--trace_dir` (default `.trace`, **independent of `--outdir`**) when
`--enable_trace` is on (the default):

| File | Content |
|---|---|
| `.trace/trace.txt` | Nextflow per-task trace: status, exit, duration, `peak_rss`, `peak_vmem`, `rchar`, `wchar` |
| `.trace/report.html` | Nextflow execution report |
| `.trace/timeline.html` | Nextflow timeline |

### The primary artifacts

<div class="outs">
  <div class="out">
    <div class="p">geojson/export/cells.geojson</div>
    <div class="role">primary · QuPath / FlowPath</div>
    <ul>
      <li>One feature per cell</li>
      <li>Whole-cell polygon + <code>nucleusGeometry</code> in compartment mode</li>
      <li>Native QuPath measurement array</li>
    </ul>
  </div>
  <div class="out">
    <div class="p">pyramid/pyramid.ome.tiff</div>
    <div class="role">primary · image</div>
    <ul>
      <li>8 levels, scale 2, zstd</li>
      <li>Channel names preserved</li>
      <li>Optional uint32 mask series (<code>embed_masks</code>)</li>
    </ul>
  </div>
  <div class="out">
    <div class="p">quantification/merged_quant.csv</div>
    <div class="role">primary · table</div>
    <ul>
      <li>One row per cell</li>
      <li>All markers × compartments × statistics</li>
      <li>Morphology joined in</li>
    </ul>
  </div>
  <div class="out">
    <div class="p">spatialdata/&lt;pid&gt;.zarr</div>
    <div class="role">additive · scverse</div>
    <ul>
      <li>Masks, polygons, AnnData table</li>
      <li>Centroids + per-cell residual</li>
      <li>QC verbatim + provenance</li>
    </ul>
  </div>
</div>

---

## The measurement-key contract

`quantify.py` produces, and `export_geojson.py` / `export_spatialdata.py` /
`phenotype_cells.py` consume, a single key grammar:

```text
"<marker>: <Compartment>: <Statistic>"
```

| Vocabulary | Values | Governed by |
|---|---|---|
| `<Compartment>` | `Nucleus`, `Cytoplasm`, `Cell` | `--quantify_compartments` — when `false`, only `Cell` is produced |
| `<Statistic>` | `Median`, `Mean`, `Sum`, `REDSEA`, each optionally suffixed ` Z` or ` RobustZ` | Whichever `--quantify_statistics` names (default `Median`). `Median`/`Mean`/`Sum` appear per compartment; `REDSEA` is whole-cell only. The `Z` variants are standardised across one patient's cells. |

So a run with `quantify_compartments=true` and `--quantify_statistics Median,Mean,Sum`
emits nine keys per marker:

```text
CD3: Nucleus: Median      CD3: Nucleus: Mean      CD3: Nucleus: Sum
CD3: Cytoplasm: Median    CD3: Cytoplasm: Mean    CD3: Cytoplasm: Sum
CD3: Cell: Median         CD3: Cell: Mean         CD3: Cell: Sum
```

Plus one **bare** column per marker (`CD3`), which is the whole-cell *mean*,
kept for backward compatibility with FlowPath's bare-key fast path.

!!! danger "Do not change this format"
    The grammar is **case- and space-sensitive** and is consumed by the sibling
    repository `qupath-extension-flowpath`. `measurement_key()` in
    `bin/utils/measurements.py` is the single producer of the string; the
    `COMPARTMENTS` and `STATISTICS` tuples are the single vocabulary. Changing
    any of the three requires a coordinated change on the FlowPath side.

Cytoplasm is computed as `Cell − Nucleus` by subtraction, per cell. A cell with
no nuclear overlap yields an empty Nucleus/Cytoplasm compartment, which is
emitted as `0.0` rather than dropped — so the row count is identical across
every compartment.

### Non-marker columns

`MORPHOLOGY_COLS` — the twelve columns in `merged_quant.csv` that are geometry
or identity rather than marker signal, and are therefore excluded from every
marker sweep:

```text
label · y · x · area · eccentricity · perimeter · convex_area
axis_major_length · axis_minor_length · solidity · fov · cell_size
```

`label` is the segmentation instance id — the **only stable cell identifier the
pipeline produces**. It is the SpatialData store's `instance_key`, so every join
is keyed on it and a mismatch is fatal rather than silently positional.

---

## See also

- :material-tune: **Every parameter** — [Parameters](parameters.md)
- :material-server: **Every resource request** — [Resources](resources.md)
- :material-database: **The `.zarr` layout in detail** — [SpatialData & FlowPath](spatialdata.md)
- :material-chart-scatter-plot: **`*_seg_qc.json` schema** — [Registration QC](registration_qc.md)
