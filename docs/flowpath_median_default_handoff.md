# Handoff: make MEDIAN the default gating statistic in qupath-extension-flowpath

**Repo:** `/Users/valer/Desktop/Github/qupath-extension-flowpath` (Java, QuPath extension)
**You have no prior context — this brief is self-contained.**

## Why

The sibling pipeline (Mirage) that produces `cells.geojson` changed its statistic policy:

- **Median is now the default per-channel statistic and is ALWAYS exported** as the
  structured measurement key `"<marker>: <Compartment>: Median"` (e.g. `"CD3: Cell: Median"`),
  for every compartment present.
- Mean and Sum are exported only when the run is "expanded": `"<marker>: <Compartment>: Mean"`
  and `"...: Sum"`.
- The **bare** `"<marker>"` column (e.g. `"CD3"`) is UNCHANGED — it is still the whole-cell
  **mean**, kept for backward compatibility and as FlowPath's bare-key fallback.
- Compartments: `Cell` is always present; `Nucleus`/`Cytoplasm` are present when compartment
  quantification ran. Measurement keys use the exact tokens `Nucleus`, `Cytoplasm`, `Cell`
  and `Mean`, `Median`, `Sum` (case-sensitive) — unchanged.

Goal: a freshly opened gate/axis should default to **Median** (resolving the structured
`"<marker>: Cell: Median"` key), while still falling back to Mean gracefully for
mean-only/legacy GeoJSONs that lack Median columns.

## The change (minimal — 5 lines, 3 files)

Change the default `Statistic` on the gate model field initializers from `MEAN` to `MEDIAN`:

1. `src/main/java/qupath/ext/flowpath/model/GateNode.java:36`
   `private Statistic statistic = Statistic.MEAN;` → `= Statistic.MEDIAN;`
2. `src/main/java/qupath/ext/flowpath/model/QuadrantGate.java:28-29`
   `statisticX` and `statisticY` `= Statistic.MEAN;` → `= Statistic.MEDIAN;`
3. `src/main/java/qupath/ext/flowpath/model/Region2DGate.java:25-26`
   `statisticX` and `statisticY` `= Statistic.MEAN;` → `= Statistic.MEDIAN;`

That is sufficient: `ui/GateEditorPane.java` `addCompartmentControls` (~line 1022) selects the
model's statistic in the ComboBox when it is among the available stats, and falls back to Mean
(`stats.get(0)`, enum-order first) when Median is unavailable — so mean-only/legacy data still
opens on Mean automatically.

## Do NOT change (verified — changing these breaks compatibility)

- The bare-key fallback in `model/CellIndex.java` — `getResolvedColumn` / `resolvedKey` /
  `isDefault` (~lines 188-191, 239-242, 250-254, 267-270). This is hard-wired so that
  `(Compartment.WHOLE_CELL, Statistic.MEAN)` resolves to the bare `"<marker>"` column, which
  IS the whole-cell mean. Leave it. Median selections resolve through the structured
  `"<marker>: Cell: Median"` key, not this fallback.
- `model/Statistic.java` `defaultStatistic()` — unused by production code (only a test refs it).
  Editing it changes nothing functionally; skip it.

## Verify

1. Build: `./gradlew build` (or the repo's usual build/test task).
2. Unit-level: a freshly constructed `GateNode` / `QuadrantGate` / `Region2DGate` reports
   `statistic == Statistic.MEDIAN`.
3. Behavior on RICH data (has `"CD3: Cell: Median"`): opening a gate on a marker shows the
   statistic selector defaulting to **Median** and reads the `: Cell: Median` column.
4. Behavior on MEAN-ONLY/legacy data (only the bare `"CD3"` column, no Median key): the gate
   still opens without error and shows **Mean** (graceful fallback).
5. Re-run the existing test suite; update any test that asserts a default of `Statistic.MEAN`
   for a freshly created gate to expect `Statistic.MEDIAN`.

## Notes

- This is one half of a coordinated two-repo change; the Mirage half (emitting the Median
  columns by default, `expanded` → Mean+Sum) is already done.
- Keep the measurement-key token spelling/case identical to Mirage: `Nucleus`/`Cytoplasm`/`Cell`,
  `Mean`/`Median`/`Sum`. A mismatch silently breaks the compartment/statistic selectors.
