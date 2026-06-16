# Single GeoJSON with per-compartment quantification + flowpath UI wiring

**Date:** 2026-06-16
**Branch:** `feature/single-geojson-compartments` (in both `mirage` and `qupath-extension-flowpath`)
**Repos:** `mirage` (Nextflow pipeline) and `qupath-extension-flowpath` (QuPath/JavaFX extension)

## Problem

When `params.quantify_compartments=true`, Mirage segments both nuclei and whole cell,
quantifies three compartments (Nucleus / Cytoplasm / Cell) per marker, and then exports
**three** GeoJSON files:

- `cells.geojson` — QuPath-native *cell* objects: whole-cell polygon as `geometry`,
  nucleus polygon as `nucleusGeometry`, all three compartment measurements.
- `nuclei.geojson` — detection objects, nucleus geometry only (redundant).
- `cells_wholecell.geojson` — detection objects, whole-cell geometry only (redundant).

The two extra files carry the same measurements as `cells.geojson` and have **no
in-pipeline or downstream consumers** (verified by grep: only the module declaration,
the Python writer, and one unit test reference them). They are redundant.

On the consuming side, `qupath-extension-flowpath` already parses the rich
`"<marker>: <Compartment>: <Mean|Median|Sum>"` measurement schema and renders
compartment/statistic ComboBoxes — but the selectors do **not** drive the plotted data:
the histogram and scatter plot read the bare whole-cell value regardless of selection.

## Goals

1. **Mirage:** when `quantify_compartments=true`, emit exactly **one** GeoJSON
   (`cells.geojson`) — a whole-cell-segmentation cell object that carries `nucleusGeometry`
   plus all three compartment quantifications. Stop emitting `nuclei.geojson` and
   `cells_wholecell.geojson`. Legacy mode (`quantify_compartments=false`) is unchanged
   (already one `cells.geojson`).
2. **flowpath:** confirm it ingests the single `cells.geojson` and that
   `CompartmentCapability.scan()` activates the selectors.
3. **flowpath:** make the compartment and mean/median/sum selectors actually drive the
   histogram and scatter plot, so the UI updates when the user changes either.

## Non-goals

- No change to segmentation, quantification math, or the measurement-key naming scheme.
- No change to legacy single-compartment behaviour.
- No new GeoJSON schema — `cells.geojson` already has the desired shape.

## Design

### Part 1 — Mirage: collapse to one GeoJSON

The user chose to keep the **cell object + nucleusGeometry** shape (richest, QuPath can
toggle nucleus/cell outline; flowpath's compartment selector keys off measurements, not
geometry). So `cells.geojson` is unchanged in content — we only stop writing the extras.

- `bin/export_geojson.py::export_compartment_geojsons()`
  - Stop building/writing `nuclei_only` and `cells_wholecell` collections and their files.
  - Keep building/writing the combined `cells.geojson` (cell objects with
    `nucleusGeometry` + all compartment measurements).
  - Return value simplifies to `{prefix: <count>}`. Update the log line.
  - Consider renaming the function (e.g. `export_combined_geojson`) for honesty, or keep
    the name to minimise churn — decided in the plan.
- `modules/local/export_geojson.nf`
  - Remove the two `optional: true` emits (`nuclei_geojson`, `wholecell_geojson`).
  - Update the header/inline comments that describe "the separate nuclei/cells files".
- `tests/unit/test_compartment_export.py`
  - Update assertions: expect only `cells.geojson`; assert it has the three compartment
    measurements and `nucleusGeometry`; assert `nuclei.geojson` /
    `cells_wholecell.geojson` are **not** written.
- `subworkflows/local/postprocess.nf` — no change expected (consumes only `.geojson` and
  `.csv`); confirm during implementation.

### Part 2 — flowpath: verify ingest

No code change anticipated. Confirm via a unit/integration check that loading a
`cells.geojson` with per-compartment measurements lights up
`CompartmentCapability.hasCompartments(channel)` and the selector lists the available
compartments/statistics. If a gap is found, fix it here.

### Part 3 — flowpath: wire selectors to plots

In `src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java`:

1. **Histogram** (`updateHistogram()`, ~1151): replace
   `cellIndex.getMarkerValues(markerIdx)` (line 1159) with
   `cellIndex.getResolvedColumn(channel, currentNode.getCompartment(), currentNode.getStatistic())`.
2. **Scatter** (`getFilteredXY()` ~1109 / `getFilteredXYWithZScore()` ~1130): resolve the
   X and Y columns via each axis's compartment/statistic
   (`getResolvedColumn(chX, gate.getCompartmentX(), gate.getStatisticX())`, etc.) instead
   of bare `getMarkerValues()`. This likely means threading channel + compartment +
   statistic into these helpers (or adding resolved-column variants).
3. **Listeners** (compartment ~1000-1006, statistic ~1027-1032): after `updateHistogram()`,
   also call `refreshScatterPlot()` so 2D gates (quadrant/polygon/rectangle/ellipse) update.

**Implementation decision — axis scaling for non-default compartments.**
`updateHistogram()`'s z-score transform (1188-1193) and clip range (1206-1213) come from
`markerStats`, which is computed per *bare* channel (whole-cell). If we plot a non-default
compartment's values against whole-cell-derived scaling, the data updates but the axis is
wrong. Options, to be resolved in the plan:
- (a) For non-default (compartment, statistic), derive the histogram clip range (and any
  z-score) from the **resolved column's own** distribution; keep `markerStats` for the
  default whole-cell-mean case (preserves cross-tree-consistent axes there).
- (b) Make `markerStats` compartment-aware (larger change).
Recommendation: (a) — smallest change that makes the displayed axis match the displayed
data. Confirm whether z-score thresholding is even offered for non-default compartments;
if not, only the clip range needs handling.

## Verification

**Mirage**
- `pytest tests/unit/test_compartment_export.py` — single file emitted, measurements +
  `nucleusGeometry` present, extras absent.
- `nextflow run . -profile test,docker -stub --outdir results` — pipeline wires cleanly
  with the reduced emits.
- nf-test export test (if present) asserts a single geojson.

**flowpath**
- `./gradlew build` (compile + existing tests).
- A test asserting that, for a loaded compartment-rich GeoJSON, the histogram data feed
  is `getResolvedColumn(channel, compartment, statistic)` for a non-default compartment
  (i.e. changing the selector changes the values), and that `refreshScatterPlot()` is
  invoked on selector change.

## Risks

- **Threading compartment/statistic through scatter helpers** touches several call sites;
  keep edits mechanical and lean on the compile + existing gating tests.
- **Axis-scaling subtlety** (above) is the main correctness risk — flagged for the plan.
- Two independent repos/branches; commit and verify each separately.
