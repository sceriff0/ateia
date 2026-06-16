# Single GeoJSON + flowpath UI wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When per-compartment quantification is on, Mirage emits one rich `cells.geojson`, and flowpath's compartment + mean/median/sum selectors actually drive the histogram and scatter plot.

**Architecture:** Two independent repos, both on branch `feature/single-geojson-compartments`. Mirage: collapse the three-file compartment export to a single combined file (rename the function). flowpath: route the existing compartment/statistic selectors into the plot data path via `CellIndex.getResolvedColumn`, derive the histogram clip range from the selected column's own distribution, and refresh the scatter plot on selector change.

**Tech Stack:** Python 3 + pandas (Mirage `bin/`), Nextflow DSL2, pytest; Java 17 + JavaFX + Gradle + JUnit (flowpath).

**Worktrees:**
- Mirage: `~/.config/superpowers/worktrees/mirage/single-geojson-compartments`
- flowpath: `~/.config/superpowers/worktrees/flowpath/single-geojson-compartments`

All `git`/`pytest`/`gradlew` commands below run from the relevant worktree root.

---

## Part 1 — Mirage: collapse to one GeoJSON

### Task 1: Single-file combined export (rename + drop extras)

**Files:**
- Modify: `bin/export_geojson.py` (`export_compartment_geojsons` ~278-358; call site ~440-452)
- Test: `tests/unit/test_compartment_export.py` (rewrite `TestCompartmentGeoJsonExport`)

- [ ] **Step 1: Rewrite the test class to expect a single combined file**

Replace the entire `TestCompartmentGeoJsonExport` class (lines 16-94) with:

```python
class TestCombinedGeoJsonExport:
    """export_geojson.export_combined_geojson — one rich cells.geojson."""

    @staticmethod
    def _df():
        return pd.DataFrame({
            'label': [1, 2],
            'x': [10.0, 20.0],
            'y': [10.0, 20.0],
            'area': [100.0, 120.0],
            'CD3: Nucleus: Mean': [100.0, 50.0],
            'CD3: Cytoplasm: Mean': [10.0, 5.0],
            'CD3: Cell: Mean': [24.0, 12.0],
        })

    @staticmethod
    def _contours():
        cell = {
            '1': [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]],
            '2': [[15, 15], [25, 15], [25, 25], [15, 25], [15, 15]],
        }
        nucleus = {'1': [[8, 8], [12, 8], [12, 12], [8, 12], [8, 8]]}
        return cell, nucleus

    def test_single_file_and_count(self, tmp_path):
        from export_geojson import export_combined_geojson

        df = self._df()
        cell_c, nuc_c = self._contours()
        markers = ['CD3: Nucleus: Mean', 'CD3: Cytoplasm: Mean', 'CD3: Cell: Mean']
        counts = export_combined_geojson(
            df, str(tmp_path), 0.325, markers, cell_c, nuc_c, prefix='cells',
        )
        assert counts == {'cells': 2}
        assert (tmp_path / 'cells.geojson').exists()
        # The redundant per-compartment files must NOT be written.
        assert not (tmp_path / 'nuclei.geojson').exists()
        assert not (tmp_path / 'cells_wholecell.geojson').exists()

    def test_combined_cell_object_has_toplevel_nucleusgeometry(self, tmp_path):
        from export_geojson import export_combined_geojson

        df = self._df()
        cell_c, nuc_c = self._contours()
        markers = ['CD3: Nucleus: Mean', 'CD3: Cytoplasm: Mean', 'CD3: Cell: Mean']
        export_combined_geojson(df, str(tmp_path), 0.325, markers, cell_c, nuc_c)

        combined = json.loads((tmp_path / 'cells.geojson').read_text())
        f0, f1 = combined['features']
        assert f0['properties']['objectType'] == 'cell'
        assert 'nucleusGeometry' in f0
        assert 'nucleusGeometry' not in f0['properties']
        assert f0['nucleusGeometry']['type'] == 'Polygon'
        assert f0['geometry']['type'] == 'Polygon'
        # Cell 2 has no nucleus -> no nucleusGeometry, still a valid cell object.
        assert 'nucleusGeometry' not in f1
        assert f1['properties']['objectType'] == 'cell'
        # All three compartment measurements carried through.
        names = [m['name'] for m in f0['properties']['measurements']]
        assert 'CD3: Nucleus: Mean' in names
        assert 'CD3: Cytoplasm: Mean' in names
        assert 'CD3: Cell: Mean' in names
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `pytest tests/unit/test_compartment_export.py::TestCombinedGeoJsonExport -v`
Expected: FAIL — `ImportError: cannot import name 'export_combined_geojson'`.

- [ ] **Step 3: Rename + simplify the function in `bin/export_geojson.py`**

Replace `export_compartment_geojsons` (lines 278-358) with this single-file version (rename, drop `nuclei_only` / `cells_wholecell`):

```python
def export_combined_geojson(
    df: pd.DataFrame,
    output_dir: str,
    pixel_size: float,
    marker_cols: List[str],
    cell_contours: Optional[Dict[str, List[List[float]]]],
    nucleus_contours: Optional[Dict[str, List[List[float]]]],
    prefix: str = 'cells',
) -> Dict[str, int]:
    """Export one combined GeoJSON for per-compartment quantification.

    Writes a single ``<prefix>.geojson`` of QuPath-native **cell** objects: the
    whole-cell polygon as ``geometry`` and the nucleus polygon as top-level
    ``nucleusGeometry``, carrying all per-compartment measurements. This is the
    file FlowPath / qUMAP / annomask consume; QuPath toggles the drawn outline
    (nucleus vs cell) natively, so no separate nuclei/whole-cell files are needed.

    ``nucleus_contours`` must be keyed by **cell label** (re-keyed upstream by
    EXTRACT_NUCLEI_PROPERTIES via ``--reference_mask``), so lookup is a plain
    identity on the cell label.
    """
    color_int = rgb_to_qupath_color(*CELL_COLOR_RGB)

    cells_combined: List[Dict] = []
    n_with_nucleus = 0
    skipped = 0

    for idx, row in df.iterrows():
        x_px = row.get('x')
        y_px = row.get('y')
        if pd.isna(x_px) or pd.isna(y_px):
            skipped += 1
            continue
        x_corner = float(x_px) + 0.5
        y_corner = float(y_px) + 0.5

        cell_id = row.get('label', idx)
        label_str = str(int(cell_id)) if pd.notna(cell_id) else str(idx)

        cell_geom = _polygon_geometry(cell_contours, label_str) or {
            "type": "Point", "coordinates": [x_corner, y_corner],
        }
        nucleus_geom = _polygon_geometry(nucleus_contours, label_str)
        if nucleus_geom is not None:
            n_with_nucleus += 1

        measurements = build_measurements(row, marker_cols, pixel_size)

        cells_combined.append(build_feature(
            measurements, cell_geom, color_int,
            object_type="cell", nucleus_geometry=nucleus_geom, object_id=None,
        ))

    out = Path(output_dir)
    combined_path = str(out / f'{prefix}.geojson')
    _write_collection(cells_combined, combined_path)

    counts = {prefix: len(cells_combined)}
    logger.info(
        f"  Combined export: {len(cells_combined)} cell objects "
        f"({n_with_nucleus} with nucleus), skipped {skipped}"
    )
    return counts
```

- [ ] **Step 4: Update the call site in `main()`**

At lines ~441-452, change the dispatch to call the renamed function and update the comment:

```python
    output_geojson = str(Path(args.output_dir) / f'{args.output_prefix}.geojson')
    if nucleus_contours is not None:
        # Per-compartment quantification: one combined cell+nucleus GeoJSON.
        counts = export_combined_geojson(
            df=cell_df,
            output_dir=args.output_dir,
            pixel_size=args.pixel_size,
            marker_cols=marker_cols,
            cell_contours=contours,
            nucleus_contours=nucleus_contours,
            prefix=args.output_prefix,
        )
        num_exported = counts[args.output_prefix]
    else:
        # Whole-cell-only export (legacy behaviour).
        num_exported = export_geojson(
            df=cell_df,
            output_path=output_geojson,
            pixel_size=args.pixel_size,
            marker_cols=marker_cols,
            contours=contours,
        )
```

- [ ] **Step 5: Run the test, verify it passes**

Run: `pytest tests/unit/test_compartment_export.py -v`
Expected: PASS — `TestCombinedGeoJsonExport` (2 tests) and the unchanged `TestNucleusReKeying` (4 tests) all green.

- [ ] **Step 6: Commit**

```bash
git add bin/export_geojson.py tests/unit/test_compartment_export.py
git commit -m ":recycle: SEGMENT export: single combined cells.geojson (drop redundant nuclei/wholecell files)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2: Update the Nextflow module emits

**Files:**
- Modify: `modules/local/export_geojson.nf` (output block 28-34; comment 41-44)

- [ ] **Step 1: Remove the two redundant emits**

Replace the output block (lines 28-34) with:

```groovy
    output:
    tuple val(meta), path("export/cells.geojson"), emit: geojson
    tuple val(meta), path("export/cells_data.csv"), emit: csv
    path "versions.yml"                            , emit: versions
    path("*.size.csv")                             , emit: size_log
```

- [ ] **Step 2: Update the inline comment (lines 41-44)**

Replace with:

```groovy
    // Per-compartment quantification: pass the nucleus contours (re-keyed to cell
    // labels) so each cell gets a nucleusGeometry in the single combined cells.geojson.
    def nucleus_arg = params.quantify_compartments ? "--nucleus_contours_json ${nucleus_contours_json}" : ''
```

- [ ] **Step 3: Confirm no other references to the removed emits**

Run: `grep -rn "nuclei_geojson\|wholecell_geojson" --include="*.nf" --include="*.config" .`
Expected: no matches (the subworkflow consumes only `.out.geojson` and `.out.csv`).

- [ ] **Step 4: Stub-run the pipeline to confirm it wires cleanly**

Run: `nextflow run . -profile test,docker -stub --outdir results_plancheck 2>&1 | tail -30`
Expected: run completes without a channel/emit error; `EXPORT_GEOJSON` present in the process list. (If Docker is unavailable in this environment, instead run `nextflow run . -profile test -stub --outdir results_plancheck` — the stub block touches `export/cells.geojson` only.)

- [ ] **Step 5: Commit**

```bash
rm -rf results_plancheck .nextflow* work
git add modules/local/export_geojson.nf
git commit -m ":recycle: EXPORT_GEOJSON: drop redundant nuclei/wholecell emits

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Part 2 — flowpath: wire selectors into histogram + scatter

> All flowpath steps run from `~/.config/superpowers/worktrees/flowpath/single-geojson-compartments`.
> File: `src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java` unless noted.

### Task 3: Histogram reads the resolved compartment/statistic column

**Files:**
- Modify: `src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java` (`updateHistogram` ~1151-1244)

- [ ] **Step 1: Resolve the column by compartment/statistic**

In `updateHistogram()`, replace line 1159:

```java
        double[] allValues = cellIndex.getMarkerValues(markerIdx);
```

with:

```java
        Compartment comp = currentNode.getCompartment();
        Statistic stat = currentNode.getStatistic();
        double[] allValues = cellIndex.getResolvedColumn(channel, comp, stat);
        boolean defaultSelection = comp == null || stat == null
                || (comp == Compartment.WHOLE_CELL && stat == Statistic.MEAN);
```

(Imports `Compartment` and `Statistic` already used in this file — confirm with `grep -n "import qupath.ext.flowpath.model.Compartment\|import qupath.ext.flowpath.model.Statistic" src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java`; add them if absent.)

Then change the `useZ` line (currently line ~1185 `boolean useZ = currentNode.isThresholdIsZScore();`) to disable z-score display for non-default selections — `markerStats` can only z-score the bare whole-cell channel, so a compartment column must be shown raw:

```java
        boolean useZ = currentNode.isThresholdIsZScore() && defaultSelection;
```

- [ ] **Step 2: Derive the clip range from the resolved column for non-default selections**

In `updateHistogram()`, replace the clip-range block (lines 1206-1213):

```java
        double pctLo = currentNode.getClipPercentileLow();
        double pctHi = currentNode.getClipPercentileHigh();
        double clipLo = markerStats != null ? markerStats.getPercentileValue(channel, pctLo) : Double.NaN;
        double clipHi = markerStats != null ? markerStats.getPercentileValue(channel, pctHi) : Double.NaN;
        if (useZ && markerStats != null && markerStats.getStd(channel) > 1e-10) {
            clipLo = markerStats.toZScore(channel, clipLo);
            clipHi = markerStats.toZScore(channel, clipHi);
        }
```

with:

```java
        double pctLo = currentNode.getClipPercentileLow();
        double pctHi = currentNode.getClipPercentileHigh();
        double clipLo;
        double clipHi;
        if (defaultSelection) {
            // Default whole-cell-mean: keep the global per-marker axis so the same
            // channel uses one axis everywhere it appears in the gate tree.
            clipLo = markerStats != null ? markerStats.getPercentileValue(channel, pctLo) : Double.NaN;
            clipHi = markerStats != null ? markerStats.getPercentileValue(channel, pctHi) : Double.NaN;
            if (useZ && markerStats != null && markerStats.getStd(channel) > 1e-10) {
                clipLo = markerStats.toZScore(channel, clipLo);
                clipHi = markerStats.toZScore(channel, clipHi);
            }
        } else {
            // Non-default compartment/statistic: markerStats only knows the bare
            // whole-cell channel, so anchor the axis on the displayed column's own
            // distribution. displayValues == rawValues here (z-score is a default-only
            // axis), so percentiles of displayValues match the plotted data.
            clipLo = percentileOf(displayValues, pctLo);
            clipHi = percentileOf(displayValues, pctHi);
        }
```

- [ ] **Step 3: Add the array-percentile helper**

Add this private static method to `GateEditorPane` (e.g. just below `updateHistogram`):

```java
    /**
     * Linear-interpolated percentile of an array (NaNs ignored). Returns NaN for
     * an empty/all-NaN input so the caller's badGlobal fallback engages.
     * Package-private so PercentileOfTest (same package) can call it directly.
     * @param pct percentile in [0,100]
     */
    static double percentileOf(double[] values, double pct) {
        if (values == null || values.length == 0) return Double.NaN;
        double[] sorted = new double[values.length];
        int n = 0;
        for (double v : values) if (!Double.isNaN(v)) sorted[n++] = v;
        if (n == 0) return Double.NaN;
        sorted = java.util.Arrays.copyOf(sorted, n);
        java.util.Arrays.sort(sorted);
        if (n == 1) return sorted[0];
        double rank = (pct / 100.0) * (n - 1);
        int lo = (int) Math.floor(rank);
        int hi = (int) Math.ceil(rank);
        if (lo == hi) return sorted[lo];
        double frac = rank - lo;
        return sorted[lo] * (1 - frac) + sorted[hi] * frac;
    }
```

- [ ] **Step 4: Unit-test the percentile helper**

Create `src/test/java/qupath/ext/flowpath/ui/PercentileOfTest.java`. The helper is private; expose it for test via reflection OR (preferred) change `percentileOf` to package-private (`static double percentileOf(...)`) so the test in the same package can call it directly. Use package-private and write:

```java
package qupath.ext.flowpath.ui;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class PercentileOfTest {

    @Test
    void medianOfOddArray() {
        double[] v = {1, 2, 3, 4, 5};
        assertEquals(3.0, GateEditorPane.percentileOf(v, 50.0), 1e-9);
    }

    @Test
    void minAndMaxPercentiles() {
        double[] v = {10, 20, 30, 40};
        assertEquals(10.0, GateEditorPane.percentileOf(v, 0.0), 1e-9);
        assertEquals(40.0, GateEditorPane.percentileOf(v, 100.0), 1e-9);
    }

    @Test
    void ignoresNaNs() {
        double[] v = {Double.NaN, 1, 2, 3, Double.NaN};
        assertEquals(2.0, GateEditorPane.percentileOf(v, 50.0), 1e-9);
    }

    @Test
    void emptyOrAllNaNReturnsNaN() {
        assertTrue(Double.isNaN(GateEditorPane.percentileOf(new double[0], 50.0)));
        assertTrue(Double.isNaN(GateEditorPane.percentileOf(new double[]{Double.NaN}, 50.0)));
    }
}
```

(The helper is declared package-private `static double percentileOf` in Step 3, so this same-package test calls it directly — no reflection needed.)

- [ ] **Step 5: Compile + run the new test**

Run: `./gradlew test --tests "qupath.ext.flowpath.ui.PercentileOfTest"`
Expected: BUILD SUCCESSFUL, 4 tests passed.

- [ ] **Step 6: Commit**

```bash
git add src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java \
        src/test/java/qupath/ext/flowpath/ui/PercentileOfTest.java
git commit -m ":bug: GateEditorPane: histogram follows selected compartment/statistic

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 4: Scatter plot resolves per-axis compartment/statistic

**Files:**
- Modify: `src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java` (`getFilteredXY` ~1109; `getFilteredXYWithZScore` ~1130; `refreshScatterPlot` ~1317; add `get2DCompartmentX/Y`, `get2DStatisticX/Y` near `get2DChannelX` ~1356)

- [ ] **Step 1: Add per-axis compartment/statistic resolvers for 2D gates**

Add these helpers next to `get2DChannelX`/`get2DChannelY` (after line ~1366). Only `QuadrantGate` carries per-axis selectors; all other 2D gates default to whole-cell mean (their current behaviour):

```java
    private Compartment get2DCompartmentX(GateNode node) {
        if (node instanceof QuadrantGate qg) return qg.getCompartmentX();
        return Compartment.WHOLE_CELL;
    }

    private Compartment get2DCompartmentY(GateNode node) {
        if (node instanceof QuadrantGate qg) return qg.getCompartmentY();
        return Compartment.WHOLE_CELL;
    }

    private Statistic get2DStatisticX(GateNode node) {
        if (node instanceof QuadrantGate qg) return qg.getStatisticX();
        return Statistic.MEAN;
    }

    private Statistic get2DStatisticY(GateNode node) {
        if (node instanceof QuadrantGate qg) return qg.getStatisticY();
        return Statistic.MEAN;
    }
```

- [ ] **Step 2: Make `getFilteredXY` resolve columns instead of using bare marker values**

Replace `getFilteredXY` (lines 1109-1124) with a channel/compartment/statistic-aware version:

```java
    private double[][] getFilteredXY(String chX, Compartment compX, Statistic statX,
                                     String chY, Compartment compY, Statistic statY) {
        double[] allX = cellIndex.getResolvedColumn(chX, compX, statX);
        double[] allY = cellIndex.getResolvedColumn(chY, compY, statY);
        boolean hasMask = roiMask != null || ancestorMask != null;
        if (!hasMask) return new double[][]{allX, allY};
        int count = 0;
        for (int i = 0; i < allX.length; i++) {
            if (passesMasks(i)) count++;
        }
        double[] fx = new double[count], fy = new double[count];
        int j = 0;
        for (int i = 0; i < allX.length; i++) {
            if (passesMasks(i)) { fx[j] = allX[i]; fy[j] = allY[i]; j++; }
        }
        return new double[][]{fx, fy};
    }
```

- [ ] **Step 3: Update `getFilteredXYWithZScore` to the new signature**

Replace `getFilteredXYWithZScore` (lines 1130-1142) with:

```java
    /**
     * Like getFilteredXY but transforms values to z-score space.
     * Used for quadrant gate scatter plots where thresholds are in z-score space.
     */
    private double[][] getFilteredXYWithZScore(String chX, Compartment compX, Statistic statX,
                                               String chY, Compartment compY, Statistic statY) {
        double[][] raw = getFilteredXY(chX, compX, statX, chY, compY, statY);
        if (markerStats == null) return raw;
        double[] fx = raw[0];
        double[] fy = raw[1];
        double[] zx = new double[fx.length];
        double[] zy = new double[fy.length];
        for (int i = 0; i < fx.length; i++) {
            zx[i] = markerStats.toZScore(chX, fx[i]);
            zy[i] = markerStats.toZScore(chY, fy[i]);
        }
        return new double[][]{zx, zy};
    }
```

- [ ] **Step 4: Update `refreshScatterPlot` to pass compartment/statistic and pick the axis range**

Replace `refreshScatterPlot` (lines 1317-1340) with:

```java
    private void refreshScatterPlot() {
        if (currentScatter == null || cellIndex == null || currentNode == null) return;
        String chX = get2DChannelX(currentNode);
        String chY = get2DChannelY(currentNode);
        if (chX == null || chY == null) return;
        int mxIdx = cellIndex.getMarkerIndex(chX);
        int myIdx = cellIndex.getMarkerIndex(chY);
        if (mxIdx < 0 || myIdx < 0) return;
        Compartment compX = get2DCompartmentX(currentNode);
        Compartment compY = get2DCompartmentY(currentNode);
        Statistic statX = get2DStatisticX(currentNode);
        Statistic statY = get2DStatisticY(currentNode);
        boolean defaultAxes =
                (compX == null || (compX == Compartment.WHOLE_CELL && statX == Statistic.MEAN))
             && (compY == null || (compY == Compartment.WHOLE_CELL && statY == Statistic.MEAN));
        // All 2D gate types (quadrant, polygon, rectangle, ellipse) use per-gate z-score flag
        double[][] filtered;
        if (currentNode.isThresholdIsZScore() && markerStats != null) {
            filtered = getFilteredXYWithZScore(chX, compX, statX, chY, compY, statY);
        } else {
            filtered = getFilteredXY(chX, compX, statX, chY, compY, statY);
        }
        currentScatter.setData(filtered[0], filtered[1], chX, chY);
        if (markerStats != null && defaultAxes) {
            // markerStats only knows the bare whole-cell channels; only anchor the
            // axis range when both axes show that default. Otherwise let the scatter
            // auto-fit to the displayed (resolved) data.
            if (currentNode.isThresholdIsZScore()) {
                applyClipAxisRangeZScore(currentScatter, chX, chY, currentNode);
            } else {
                applyClipAxisRange(currentScatter, chX, chY, currentNode);
            }
        } else {
            currentScatter.clearAxisRange();
        }
    }
```

- [ ] **Step 5: Compile to confirm signatures line up**

Run: `./gradlew compileJava`
Expected: BUILD SUCCESSFUL. (If it fails, the only legitimate cause is another caller of the old `getFilteredXY(int,int)` / `getFilteredXYWithZScore(int,int,...)` signatures — search with `grep -n "getFilteredXY" src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java` and update those call sites to the new signature; `refreshScatterPlot` is expected to be the only caller.)

- [ ] **Step 6: Commit**

```bash
git add src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java
git commit -m ":bug: GateEditorPane: scatter resolves per-axis compartment/statistic

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5: Selector changes refresh the scatter plot

**Files:**
- Modify: `src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java` (`addCompartmentControls` listeners ~1000-1006 and ~1027-1032)

- [ ] **Step 1: Call `refreshScatterPlot()` from the compartment listener**

Replace the compartment `setOnAction` (lines ~1000-1006):

```java
        compCombo.setOnAction(e -> {
            if (!suppressEvents && currentNode != null) {
                setComp.accept(compCombo.getValue());
                updateHistogram();
                refreshScatterPlot();
                fireNodeChanged();
            }
        });
```

- [ ] **Step 2: Call `refreshScatterPlot()` from the statistic listener**

Replace the statistic `setOnAction` (lines ~1027-1032):

```java
        statCombo.setOnAction(e -> {
            if (!suppressEvents && currentNode != null) {
                setStat.accept(statCombo.getValue());
                updateHistogram();
                refreshScatterPlot();
                fireNodeChanged();
            }
        });
```

- [ ] **Step 3: Build the whole module + run the full test suite**

Run: `./gradlew build`
Expected: BUILD SUCCESSFUL; existing suites (`CompartmentGatingTest`, `CellIndexTest`, `GateNodeTest`, `CompartmentModelTest`, etc.) and the new `PercentileOfTest` all pass.

- [ ] **Step 4: Commit**

```bash
git add src/main/java/qupath/ext/flowpath/ui/GateEditorPane.java
git commit -m ":bug: GateEditorPane: refresh scatter when compartment/statistic changes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 6: Manual smoke verification (cross-repo)

This confirms the two repos work together; it is a manual step (no automated harness drives QuPath + a real run).

- [ ] **Step 1: Produce a compartment GeoJSON from Mirage**

From the Mirage worktree, run a real (non-stub) compartment export on test data if available, or reuse an existing `cells.geojson` containing `"<marker>: Nucleus: Mean"` keys. Confirm exactly one `cells.geojson` is produced under `<outdir>/<patient>/geojson/` and that no `nuclei.geojson` / `cells_wholecell.geojson` appear.

- [ ] **Step 2: Load it in QuPath + flowpath**

Build the flowpath extension (`./gradlew build`), load the extension and the `cells.geojson` into QuPath, open FlowPath. Confirm:
  - the **Signal** (compartment) ComboBox appears for a marker with rich measurements;
  - changing the compartment **redraws the histogram** (different distribution) for a threshold gate;
  - for a quadrant gate, changing compartment/statistic on an axis **redraws the scatter**;
  - when expanded quantification provides Median/Sum, the statistic ComboBox switches the plotted values.

- [ ] **Step 2 fallback (if no QuPath GUI available):** document that GUI verification is pending and rely on `./gradlew build` + `CompartmentGatingTest` (engine-level compartment resolution) as the automated proxy. Note this explicitly in the PR description.

---

## Notes / decisions locked from the spec

- Output filename stays `cells.geojson` (flowpath import + module path depend on it); only the **Python function** is renamed `export_compartment_geojsons` → `export_combined_geojson`.
- Histogram clip range for non-default compartment/statistic comes from the **resolved column's own distribution** (`percentileOf`), not `markerStats`. Same philosophy applied to the scatter axis range (auto-fit when non-default).
- z-score display remains a default-selection (whole-cell) axis; non-default selections plot raw resolved values. If product later wants z-score on a per-compartment basis, `markerStats` must become compartment-aware (out of scope here).
