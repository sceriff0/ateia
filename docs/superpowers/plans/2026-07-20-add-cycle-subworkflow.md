# ADD_CYCLE Subworkflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an incremental cyclic-IF mode that folds a new imaging cycle's markers into an already-completed patient result, reusing the prior reference, segmentation mask, and old-marker quantification instead of reprocessing the whole patient.

**Architecture:** A new self-contained subworkflow `ADD_CYCLE` (`subworkflows/local/add_cycle.nf`), selected by `params.mode = 'add_cycle'`, bypasses the linear `preprocessing→registration→postprocessing` step gate. It preprocesses and registers only the new cycle against the frozen prior reference, reuses the prior `cell_mask` (no `SEGMENT`), quantifies only the new markers, then regenerates a complete `cells.geojson` and pyramid from the combined channel/measurement set. Every stage reuses an existing module; the only Python change is collision-aware merging in `bin/merge_quant_csvs.py`.

**Tech Stack:** Nextflow DSL2 (>=25.04.0, nf-boost), Groovy (`lib/`), Python 3 (`bin/`, pandas), nf-test + pytest.

## Global Constraints

- Nextflow `>=25.04.0`; one process per file in `modules/local/`; UPPER_SNAKE_CASE process names.
- Every process uses a container with an immutable tag (never `:latest`); resources via labels scaling with `task.attempt`.
- Tool arguments live in `conf/modules.config` via `ext.args`, never hardcoded in process scripts.
- All QC is non-gating (`base.config` error strategy); the incremental mode must not make QC gating.
- Meta map pattern: channels carry `[meta, file(s)]`; meta includes `id, patient_id, channel_name, is_reference, channels`.
- The existing 3-step flow (`params.start`/`params.stop`) must remain byte-for-byte unchanged when `params.mode != 'add_cycle'`.
- FlowPath GeoJSON contract: measurement keys are case-sensitive `marker: Compartment: Statistic`; do not rename existing marker columns.
- New-cycle marker collision rule: **new cycle wins (overwrites the prior same-named column), except `DAPI`, which is never overwritten** (the reference DAPI anchor is protected).
- No new `bin/` scripts are introduced (so no new exec-bit management); only `merge_quant_csvs.py` is edited.

---

## Execution Order (amended 2026-07-20 — mask-carrying pyramid)

The mask-series work was added after initial planning. Dispatch order:
**Task 1 (done) → Task 2 → Task 6 → Task 7 → Task 3 → Task 4 → Task 5.**
Tasks 6 (pyramid writer) and 7 (mask reader/validation) must land before
Task 3 (the subworkflow that consumes the extracted masks). Task 3's mask
sourcing is revised by the note at the head of Task 3.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `bin/merge_quant_csvs.py` | Merge per-marker CSVs onto a base table by `label`; collision-aware (new-wins-except-DAPI) | Modify `merge_intensities` |
| `tests/test_merge_quant_csvs.py` | Pytest unit tests for the merge collision logic | Create |
| `lib/ParamUtils.groovy` | Validate `add_cycle` mode params | Modify (add `validateAddCycle`) |
| `lib/CsvUtils.groovy` | (reused as-is for new-cycle CSV validation) | No change |
| `subworkflows/local/add_cycle.nf` | `ADD_CYCLE` orchestration: preprocess→register→reuse mask→quantify→merge→export→pyramid | Create |
| `workflows/mirage.nf` | Branch to `ADD_CYCLE` when `params.mode == 'add_cycle'`; build prior-asset channels from prior outdir | Modify |
| `nextflow.config` | Declare `params.mode`, `params.prior_outdir` defaults | Modify |
| `conf/modules.config` | publishDir for `ADD_CYCLE`-mode outputs (fresh outdir) | Modify (reuse existing patient-dir patterns) |
| `tests/subworkflows/add_cycle.nf.test` | nf-test stub coverage of `ADD_CYCLE` | Create |
| `CLAUDE.md` / `docs` | Document the mode | Modify |

---

## Task 1: Collision-aware merge in `merge_quant_csvs.py`

**Files:**
- Modify: `bin/merge_quant_csvs.py` (`merge_intensities`, ~lines 52-86)
- Test: `tests/test_merge_quant_csvs.py` (create)

**Interfaces:**
- Consumes: nothing (leaf change).
- Produces: `merge_intensities(morphology: pd.DataFrame, csv_files: list[Path], protected_cols: tuple[str, ...] = ('DAPI',)) -> pd.DataFrame` — when an incoming marker column already exists in the base, the incoming value replaces it (new wins), unless the column is in `protected_cols`, in which case the base value is kept and the incoming column dropped. `main()` passes `protected_cols=('DAPI',)`. The existing `--morphology` argument is reused as the "base table" slot (it may now be a full prior merged CSV, not just morphology).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_merge_quant_csvs.py`:

```python
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

# Import the bin script as a module (it inserts bin/utils on sys.path at import).
_SPEC = importlib.util.spec_from_file_location(
    "merge_quant_csvs",
    Path(__file__).resolve().parent.parent / "bin" / "merge_quant_csvs.py",
)
mqc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mqc)


def _write_csv(tmp_path, name, frame):
    p = tmp_path / name
    frame.to_csv(p, index=False)
    return p


def test_new_marker_appends_as_column(tmp_path):
    base = pd.DataFrame({"label": [1, 2, 3], "area": [10, 20, 30]})
    new = _write_csv(tmp_path, "c2_CD8_quant.csv",
                     pd.DataFrame({"label": [1, 2, 3], "CD8": [5.0, 6.0, 7.0]}))
    out = mqc.merge_intensities(base, [new])
    assert list(out["CD8"]) == [5.0, 6.0, 7.0]
    assert len(out) == 3  # no cells gained/lost


def test_colliding_marker_new_cycle_wins(tmp_path):
    # Base already has a PANCK column (from cycle 1); new cycle re-measures PANCK.
    base = pd.DataFrame({"label": [1, 2], "area": [10, 20], "PANCK": [1.0, 2.0]})
    new = _write_csv(tmp_path, "c2_PANCK_quant.csv",
                     pd.DataFrame({"label": [1, 2], "PANCK": [99.0, 98.0]}))
    out = mqc.merge_intensities(base, [new])
    # New cycle overwrites; no pandas _x/_y suffix columns survive.
    assert "PANCK_x" not in out.columns and "PANCK_y" not in out.columns
    assert list(out["PANCK"]) == [99.0, 98.0]


def test_dapi_is_protected_from_overwrite(tmp_path):
    base = pd.DataFrame({"label": [1, 2], "DAPI": [100.0, 200.0]})
    new = _write_csv(tmp_path, "c2_DAPI_quant.csv",
                     pd.DataFrame({"label": [1, 2], "DAPI": [1.0, 2.0]}))
    out = mqc.merge_intensities(base, [new], protected_cols=("DAPI",))
    # Base DAPI kept, incoming ignored.
    assert list(out["DAPI"]) == [100.0, 200.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_merge_quant_csvs.py -v`
Expected: `test_colliding_marker_new_cycle_wins` FAILS (asserts see `PANCK_x`/`PANCK_y` because the current code has no collision handling); `test_dapi_is_protected_from_overwrite` FAILS. (`test_new_marker_appends_as_column` may already pass.)

- [ ] **Step 3: Implement collision handling**

In `bin/merge_quant_csvs.py`, replace the `merge_intensities` signature and body's per-file loop head with collision handling. New function:

```python
def merge_intensities(
    morphology: pd.DataFrame,
    csv_files: list[Path],
    protected_cols: tuple[str, ...] = ('DAPI',),
) -> pd.DataFrame:
    """Merge each intensity CSV into the base table by label.

    `morphology` is the base table: a morphology-only table for a normal run,
    or a full prior merged table for an incremental (add_cycle) run. When an
    incoming marker column already exists in the base, the incoming value wins
    (overwrites), UNLESS the column is in `protected_cols` (e.g. DAPI), in which
    case the base is kept and the incoming column is dropped.
    """
    merged = morphology.copy()
    morphology_cells = set(merged['label'])

    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        marker_cols = [col for col in df.columns if col != 'label']

        if not marker_cols:
            logger.warning("%s: No marker columns found, skipping", csv_file.name)
            continue

        # Collision handling: for any marker already present in the base table.
        for col in list(marker_cols):
            if col in merged.columns:
                if col in protected_cols:
                    df = df.drop(columns=[col])
                    logger.info("%s: protected column '%s' kept from base (incoming dropped)",
                                csv_file.name, col)
                else:
                    merged = merged.drop(columns=[col])
                    logger.info("%s: column '%s' overwritten by new cycle (new wins)",
                                csv_file.name, col)
        marker_cols = [col for col in df.columns if col != 'label']
        if not marker_cols:
            continue

        # Validate cell labels
        intensity_cells = set(df['label'])
        missing = morphology_cells - intensity_cells
        extra = intensity_cells - morphology_cells
        if missing:
            logger.warning("%s: Missing %d cells from base", csv_file.name, len(missing))
        if extra:
            logger.warning("%s: Has %d extra cells (will be ignored)", csv_file.name, len(extra))

        merge_df = df[['label'] + marker_cols]
        merged = merged.merge(merge_df, on='label', how='left')
        logger.info("  + %s from %s", ', '.join(marker_cols), csv_file.name)

    return merged
```

Then in `main()`, update the call site (currently `merged = merge_intensities(morphology, csv_files)`) to pass the protected columns explicitly:

```python
    merged = merge_intensities(morphology, csv_files, protected_cols=('DAPI',))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_merge_quant_csvs.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Regression-check the normal path is unchanged**

Run: `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration`
Expected: PASS. (Normal runs pass a morphology-only base with no marker columns, so no collision branch ever fires — behavior is identical.)

- [ ] **Step 6: Commit**

```bash
git add bin/merge_quant_csvs.py tests/test_merge_quant_csvs.py
git commit -m ":sparkles: Collision-aware merge base for incremental quantification"
```

---

## Task 2: Declare `add_cycle` mode params + validation

**Files:**
- Modify: `nextflow.config` (params block)
- Modify: `lib/ParamUtils.groovy` (add `validateAddCycle`)
- Test: `tests/subworkflows/add_cycle.nf.test` is added in Task 4; this task is verified by the Groovy validator being exercised through Task 4's stub run. For an isolated check, use the inline Groovy assertion in Step 3.

**Interfaces:**
- Consumes: nothing.
- Produces: `params.mode` (default `'standard'`), `params.prior_outdir` (default `null`). `ParamUtils.validateAddCycle(String priorOutdir)` throws `IllegalArgumentException` when `prior_outdir` is null/blank, and `FileNotFoundException` when `<prior_outdir>/csv/registered.csv` or `<prior_outdir>/csv/postprocessed.csv` is absent.

- [ ] **Step 1: Add param defaults**

In `nextflow.config`, inside the `params { … }` block, add:

```groovy
    // Incremental cyclic-IF mode. 'standard' = normal start/stop pipeline;
    // 'add_cycle' = fold a new imaging cycle into a prior completed run.
    mode         = 'standard'
    // For mode='add_cycle': the --outdir of the previously completed run,
    // whose csv/registered.csv and csv/postprocessed.csv provide the reusable
    // reference, segmentation mask, merged quantification table, and pyramid.
    prior_outdir = null
    // Embed the segmentation masks as a SECOND series in the pyramid OME-TIFF
    // (cell_mask + nuclei_mask, uint32, single-res). Written only when this is
    // true AND quantify_compartments AND expanded_quantification. When false ->
    // intensity-only pyramid (current behavior). See docs/add_cycle.md.
    embed_masks  = true
```

- [ ] **Step 2: Add the validator**

In `lib/ParamUtils.groovy`, add a method (after `validateStop`):

```groovy
    static void validateAddCycle(String priorOutdir) {
        if (!priorOutdir?.trim()) {
            throw new IllegalArgumentException(
                "mode='add_cycle' requires --prior_outdir pointing at the previous run's --outdir")
        }
        ['csv/registered.csv', 'csv/postprocessed.csv'].each { rel ->
            def f = new File("${priorOutdir}/${rel}")
            if (!f.exists()) {
                throw new FileNotFoundException(
                    "mode='add_cycle': required checkpoint '${rel}' not found under --prior_outdir '${priorOutdir}'. " +
                    "Was the prior run completed through postprocessing?")
            }
        }
    }
```

- [ ] **Step 3: Verify the validator with an inline Groovy assertion**

Run:
```bash
groovy -e "\
def cls = new GroovyClassLoader().parseClass(new File('lib/ParamUtils.groovy')); \
try { cls.validateAddCycle(null); assert false } catch (IllegalArgumentException e) { assert e.message.contains('prior_outdir') }; \
try { cls.validateAddCycle('/does/not/exist'); assert false } catch (FileNotFoundException e) { assert e.message.contains('registered.csv') }; \
println 'validateAddCycle OK'"
```
Expected: prints `validateAddCycle OK`. (If `groovy` is unavailable locally, defer this check to Task 4's nf-test which exercises the same path.)

- [ ] **Step 4: Commit**

```bash
git add nextflow.config lib/ParamUtils.groovy
git commit -m ":sparkles: Add mode=add_cycle params and validation"
```

---

## Task 3: `ADD_CYCLE` subworkflow

> **AMENDED (mask-carrying pyramid):** `ch_prior_assets` no longer carries
> `cell_mask`/`nuclei_mask` as file paths sourced from separate published TIFFs.
> Instead, Task 4's routing extracts them from the prior pyramid's `Image:1`
> series via Task 7's `EXTRACT_MASK_SERIES` process (fallback to separate mask
> files only if the pyramid has no mask series). The `ADD_CYCLE` subworkflow
> body below is unchanged — it still receives `ch_prior_assets` as a 7-tuple
> `[patient_id, ref_channels, ref_image, base_merged_csv, cell_mask, nuclei_mask, pyramid]`;
> only *where those mask entries come from* changes, and that wiring lives in
> Task 4. Do not re-derive masks inside the subworkflow.

**Files:**
- Create: `subworkflows/local/add_cycle.nf`

**Interfaces:**
- Consumes (module signatures, unchanged):
  - `PREPROCESSING(ch_input: [meta, file]) -> out.preprocessed: [meta, file]`
  - `VALIS_ADAPTER(ch_grouped: [patient_id, ref_item, all_items]) -> out.registered: [meta, file]`, `out.registrar: [patient_id, pickle]`
  - `EXTRACT_CELL_PROPERTIES([meta, cell_mask]) -> out.contours: [meta, contours.json]`, `out.morphology: [meta, morphology.csv]`
  - `EXTRACT_NUCLEI_PROPERTIES([meta, nuclei_mask, cell_mask]) -> out.contours: [meta, contours.json]` (cell-label-keyed nucleus polygons)
  - `SPLIT_CHANNELS([meta, image, is_reference]) -> out.channels: [meta, [tiffs]]`
  - `QUANTIFY([meta, channel_tiff, cell_mask, nuclei_mask]) -> out.individual_csv: [meta, csv]`
  - `MERGE_QUANT_CSVS([meta, [individual_csvs], base_csv]) -> out.merged_csv: [meta, merged_quant.csv]`
  - `EXPORT_GEOJSON([meta, quant_csv, contours_json, nucleus_contours_json]) -> out.geojson, out.csv`
  - `MERGE_AND_PYRAMID([meta, [split_channels]]) -> out.pyramid: [meta, pyramid.ome.tiff]`
  - `GENERATE_REGISTRATION_QC([meta, registered_file, reference_file]) -> out.qc`
  - `SEG_QC_GEOJSON([meta, image]) -> out.geojson: [meta, *.geojson]`
  - `WARP_SEG_QC([meta, pickle, ref_slide, moving_slide, ref_geojson, moving_geojson]) -> out.metrics: [meta, *_seg_qc.json]`
- Produces: `ADD_CYCLE(ch_new_input, ch_prior_assets)` emitting `geojson`, `merged_csv`, `pyramid`, `qc`, `seg_qc`, `versions`, `size_logs`.
  - `ch_new_input`: `[meta, raw_file]` for the new cycle (is_reference=false in meta).
  - `ch_prior_assets`: `[patient_id, ref_channels(List), ref_image, base_merged_csv, cell_mask, nuclei_mask, pyramid]`.
- Optional-path behavior: `reg_qc >= 2` adds `SEG_QC_GEOJSON` + `WARP_SEG_QC` (emits `seg_qc`); `params.quantify_compartments` adds `EXTRACT_NUCLEI_PROPERTIES` and routes the reused nuclei mask into `QUANTIFY` + nucleus contours into `EXPORT_GEOJSON`.

- [ ] **Step 1: Create the subworkflow file**

Create `subworkflows/local/add_cycle.nf`:

```groovy
/*
========================================================================================
    SUBWORKFLOW: ADD_CYCLE  (incremental cyclic-IF)
========================================================================================
    Folds a NEW imaging cycle into a prior completed patient result. Reuses the
    prior reference (registration target), the prior segmentation cell + nuclei
    masks (no SEGMENT), and the prior merged quantification table (base).
    Recomputes only: preprocessing + registration of the new slide, quantification
    of the new markers, and a wholesale regenerate of cells.geojson + the pyramid
    over the COMBINED channel/measurement set.

    Assumes true cyclic IF: the same physical section re-imaged, so the prior
    masks' cell labels align with the newly registered cycle. Registration-drift
    QC surfaces misalignment; it is non-gating:
      - reg_qc >= 1: DAPI-overlay image QC (GENERATE_REGISTRATION_QC)
      - reg_qc >= 2: + seg-overlap Dice/IoU (SEG_QC_GEOJSON -> WARP_SEG_QC),
        using the classic VALIS registrar pickle from the 2-node graph.

    Per-compartment (expanded) quantification is supported: when
    params.quantify_compartments, the reused nuclei mask feeds QUANTIFY and
    EXTRACT_NUCLEI_PROPERTIES supplies nucleus contours for EXPORT_GEOJSON.
========================================================================================
*/

include { PREPROCESSING            } from './preprocess'
include { VALIS_ADAPTER            } from './adapters/valis_adapter'
include { EXTRACT_CELL_PROPERTIES  } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { SPLIT_CHANNELS as SPLIT_PRIOR_PYRAMID } from '../../modules/local/split_channels'
include { QUANTIFY                 } from '../../modules/local/quantify'
include { MERGE_QUANT_CSVS         } from '../../modules/local/quantify'
include { EXPORT_GEOJSON           } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID        } from '../../modules/local/merge_and_pyramid'
include { GENERATE_REGISTRATION_QC } from '../../modules/local/generate_registration_qc'
include { SEG_QC_GEOJSON           } from '../../modules/local/seg_qc_geojson'
include { WARP_SEG_QC              } from '../../modules/local/warp_seg_qc'

workflow ADD_CYCLE {
    take:
    ch_new_input     // [meta, raw_file]  (new cycle slides, is_reference=false)
    ch_prior_assets  // [patient_id, ref_channels(List), ref_image, base_merged_csv, cell_mask, nuclei_mask, pyramid]

    main:
    // ------------------------------------------------------------------ //
    // 1. PREPROCESS the new cycle (mirrors cycle-1 illumination/prep)
    // ------------------------------------------------------------------ //
    PREPROCESSING(ch_new_input)
    ch_new_pre = PREPROCESSING.out.preprocessed   // [meta, file]

    // ------------------------------------------------------------------ //
    // 2. REGISTER new cycle -> frozen prior reference (2-node VALIS graph)
    // ------------------------------------------------------------------ //
    // Build [patient_id, ref_item, all_items] with the prior reference marked
    // is_reference=true. The reference is a fixed frame (passed through
    // unregistered by VALIS); we discard its output and keep only the new cycle.
    ch_ref_item = ch_prior_assets.map { pid, ref_channels, ref_image, _m, _cm, _nm, _py ->
        def ref_meta = [patient_id: pid, id: "${pid}_reference", is_reference: true, channels: ref_channels]
        [pid, [ref_meta, ref_image]]
    }
    ch_grouped = ch_new_pre
        .map { meta, file -> [meta.patient_id, [meta + [is_reference: false], file]] }
        .combine(ch_ref_item, by: 0)
        .map { pid, new_item, ref_item ->
            [pid, ref_item, [ref_item, new_item]]   // all_items = reference + new cycle
        }

    VALIS_ADAPTER(ch_grouped)

    // Keep only the newly registered cycle (drop the reference passthrough).
    ch_new_registered = VALIS_ADAPTER.out.registered.filter { meta, _f -> !meta.is_reference }

    // ------------------------------------------------------------------ //
    // 3. REGISTRATION-DRIFT QC (non-gating)
    // ------------------------------------------------------------------ //
    ch_qc     = Channel.empty()
    ch_seg_qc = Channel.empty()
    def reg_qc_level = params.skip_registration_qc ? 0 : (params.reg_qc == null ? 1 : (params.reg_qc as int))

    // Level >= 1: DAPI-overlay image QC — new registered vs prior reference.
    if (reg_qc_level >= 1) {
        ch_for_qc = ch_new_registered
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(ch_prior_assets.map { pid, _rc, ref_image, _m, _cm, _nm, _py -> [pid, ref_image] }, by: 0)
            .map { _pid, meta, reg_f, ref_f -> [meta, reg_f, ref_f] }
        GENERATE_REGISTRATION_QC(ch_for_qc)
        ch_qc = GENERATE_REGISTRATION_QC.out.qc
    }

    // Level >= 2: seg-overlap Dice/IoU. Segment DAPI on the NATIVE (pre-reg)
    // reference + new-cycle images -> cell GeoJSON, then warp through the
    // classic registrar pickle (available: ADD_CYCLE uses VALIS_ADAPTER).
    // Mirrors registration.nf:263-284.
    if (reg_qc_level >= 2) {
        ch_native = ch_new_pre
            .map { meta, f -> [meta + [is_reference: false], f] }
            .mix(ch_prior_assets.map { pid, rc, ref_image, _m, _cm, _nm, _py ->
                [[patient_id: pid, id: "${pid}_reference", is_reference: true, channels: rc], ref_image]
            })
        SEG_QC_GEOJSON(ch_native)

        ch_gj = SEG_QC_GEOJSON.out.geojson.branch { meta, gj ->
            reference: meta.is_reference
            moving:    !meta.is_reference
        }
        ch_ref_gj = ch_gj.reference.map { meta, gj -> [meta.patient_id, gj, gj.simpleName] }
        ch_mov_gj = ch_gj.moving.map    { meta, gj -> [meta.patient_id, meta, gj, gj.simpleName] }

        ch_for_warp = ch_mov_gj
            .combine(ch_ref_gj, by: 0)
            .combine(VALIS_ADAPTER.out.registrar, by: 0)
            .map { pid, meta, mov_gj, mov_name, ref_gj, ref_name, pickle ->
                tuple(meta, pickle, ref_name, mov_name, ref_gj, mov_gj)
            }
        WARP_SEG_QC(ch_for_warp)
        ch_seg_qc = WARP_SEG_QC.out.metrics
    }

    // ------------------------------------------------------------------ //
    // 4. REUSE prior masks -> cell contours (+ nucleus contours if compartments)
    //    Masks are unchanged, so cell labels are identical.
    // ------------------------------------------------------------------ //
    ch_prior_cell_mask = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, _nm, _py ->
        [[patient_id: pid, id: pid, is_reference: true], cell_mask]
    }
    EXTRACT_CELL_PROPERTIES(ch_prior_cell_mask)
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }

    // Nucleus contours (re-keyed to cell labels) — only when compartments enabled.
    ch_nucleus_contours = Channel.empty()
    if (params.quantify_compartments) {
        ch_nuclei_props_in = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, nuclei_mask, _py ->
            [[patient_id: pid, id: pid], nuclei_mask, cell_mask]
        }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }
    }

    // ------------------------------------------------------------------ //
    // 5. SPLIT new registered cycle -> per-marker single-channel TIFFs
    //    (SPLIT_CHANNELS drops the new cycle's DAPI: non-reference input.)
    // ------------------------------------------------------------------ //
    SPLIT_CHANNELS(ch_new_registered.map { meta, f -> [meta, f, false] })

    ch_new_channels = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            (tiffs instanceof List ? tiffs : [tiffs]).collect { tiff ->
                def m = meta.clone()
                m.id = "${meta.patient_id}_${tiff.baseName}"
                m.channel_name = tiff.baseName
                [m, tiff]
            }
        }

    // ------------------------------------------------------------------ //
    // 6. QUANTIFY new markers against the REUSED masks. QUANTIFY reads
    //    params.quantify_compartments to route the nuclei mask; --expanded
    //    arrives via ext.args (conf/modules.config), so no change here.
    // ------------------------------------------------------------------ //
    ch_masks = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, nuclei_mask, _py ->
        [pid, cell_mask, nuclei_mask]
    }
    ch_for_quant = ch_new_channels
        .map { meta, tiff -> [meta.patient_id, meta, tiff] }
        .combine(ch_masks, by: 0)
        .map { _pid, meta, tiff, cell_mask, nuclei_mask -> [meta, tiff, cell_mask, nuclei_mask] }
    QUANTIFY(ch_for_quant)

    // ------------------------------------------------------------------ //
    // 7. MERGE new marker CSVs onto the prior merged table (base) by label
    // ------------------------------------------------------------------ //
    ch_new_quant_grouped = QUANTIFY.out.individual_csv
        .map { meta, csv -> [meta.patient_id, csv] }
        .groupTuple()   // all new-marker CSVs for the patient
    ch_base = ch_prior_assets.map { pid, _rc, _ri, base_csv, _cm, _nm, _py -> [pid, base_csv] }
    ch_for_merge = ch_new_quant_grouped
        .combine(ch_base, by: 0)
        .map { pid, csvs, base_csv -> [[patient_id: pid, id: pid], csvs, base_csv] }
    MERGE_QUANT_CSVS(ch_for_merge)

    // ------------------------------------------------------------------ //
    // 8. EXPORT complete cells.geojson from the COMBINED table.
    //    nucleus slot: real nucleus contours when compartments enabled,
    //    else the cell contours (harmless placeholder — EXPORT_GEOJSON only
    //    passes --nucleus_contours_json under params.quantify_compartments).
    // ------------------------------------------------------------------ //
    ch_nuc_for_export = params.quantify_compartments ? ch_nucleus_contours : ch_contours
    ch_for_export = MERGE_QUANT_CSVS.out.merged_csv
        .map { meta, csv -> [meta.patient_id, meta, csv] }
        .join(ch_contours, by: 0)
        .join(ch_nuc_for_export, by: 0)
        .map { _pid, meta, csv, contours, nucleus_contours -> [meta, csv, contours, nucleus_contours] }
    EXPORT_GEOJSON(ch_for_export)

    // ------------------------------------------------------------------ //
    // 9. REBUILD complete pyramid: recover prior channels from the prior
    //    pyramid (is_reference=true keeps ref DAPI + all old markers), then
    //    merge with the new cycle's channels.
    // ------------------------------------------------------------------ //
    ch_prior_pyramid = ch_prior_assets.map { pid, _rc, _ri, _m, _cm, _nm, pyramid ->
        [[patient_id: pid, id: pid, is_reference: true, channels: []], pyramid, true]
    }
    SPLIT_PRIOR_PYRAMID(ch_prior_pyramid)

    ch_all_channels = SPLIT_CHANNELS.out.channels
        .mix(SPLIT_PRIOR_PYRAMID.out.channels)
        .flatMap { meta, tiffs ->
            (tiffs instanceof List ? tiffs : [tiffs]).collect { tiff -> [meta.patient_id, tiff.baseName, tiff] }
        }
        .unique { pid, marker, _t -> [pid, marker] }   // new cycle already excludes DAPI; keep one per marker
        .map { pid, _marker, tiff -> [pid, tiff] }
        .groupTuple()
        .map { pid, tiffs -> [[patient_id: pid, id: pid, is_reference: false], tiffs] }
    MERGE_AND_PYRAMID(ch_all_channels)

    // ------------------------------------------------------------------ //
    // Versions + size logs
    // ------------------------------------------------------------------ //
    ch_versions = Channel.empty()
        .mix(PREPROCESSING.out.versions)
        .mix(VALIS_ADAPTER.out.versions)
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(QUANTIFY.out.versions.first())
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(EXPORT_GEOJSON.out.versions.first())
        .mix(MERGE_AND_PYRAMID.out.versions.first())

    ch_size_logs = Channel.empty()
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(SPLIT_PRIOR_PYRAMID.out.size_log)
        .mix(QUANTIFY.out.size_log)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(EXPORT_GEOJSON.out.size_log)
        .mix(MERGE_AND_PYRAMID.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)

    if (reg_qc_level >= 1) {
        ch_versions  = ch_versions.mix(GENERATE_REGISTRATION_QC.out.versions.first())
        ch_size_logs = ch_size_logs.mix(GENERATE_REGISTRATION_QC.out.size_log)
    }
    if (reg_qc_level >= 2) {
        ch_versions  = ch_versions.mix(SEG_QC_GEOJSON.out.versions.first()).mix(WARP_SEG_QC.out.versions.first())
        ch_size_logs = ch_size_logs.mix(SEG_QC_GEOJSON.out.size_log).mix(WARP_SEG_QC.out.size_log)
    }
    if (params.quantify_compartments) {
        ch_versions  = ch_versions.mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    emit:
    geojson     = EXPORT_GEOJSON.out.geojson
    merged_csv  = MERGE_QUANT_CSVS.out.merged_csv
    pyramid     = MERGE_AND_PYRAMID.out.pyramid
    qc          = ch_qc
    seg_qc      = ch_seg_qc
    versions    = ch_versions
    size_logs   = ch_size_logs
}
```

- [ ] **Step 2: Syntax-check the subworkflow parses**

Run: `nextflow inspect . -profile test,docker 2>&1 | head -5` *(or)* rely on Task 4's stub run to compile it.
Expected: no Groovy parse error referencing `add_cycle.nf`.

- [ ] **Step 3: Commit**

```bash
git add subworkflows/local/add_cycle.nf
git commit -m ":sparkles: Add ADD_CYCLE incremental cyclic-IF subworkflow"
```

---

## Task 4: Route `mode=add_cycle` in the main workflow + publishDir + nf-test

> **AMENDED (mask-carrying pyramid):** the prior masks are sourced from the
> prior pyramid's second series via `EXTRACT_MASK_SERIES` (Task 7), not from
> derived `*_cell_mask.tif` paths. Replace the `ch_prior_post` mask-derivation
> in Step 1 with the block below. Include:
> `include { EXTRACT_MASK_SERIES } from '../modules/local/extract_mask_series'`.
>
> ```groovy
> // postprocessed.csv: patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid
> def ch_prior_rows = Channel
>     .fromPath("${params.prior_outdir}/csv/postprocessed.csv", checkIfExists: true)
>     .splitCsv(header: true)
>     .map { row -> [row.patient_id, file(row.merged_csv), file(row.cell_mask), file(row.pyramid)] }
>
> // Extract masks from the prior pyramid's Image:1 series (fast-fails in the
> // process if the prior run was not embed_masks+expanded+compartment).
> EXTRACT_MASK_SERIES(ch_prior_rows.map { pid, _mc, _cm, pyramid -> [[patient_id: pid, id: pid], pyramid] })
> def ch_masks = EXTRACT_MASK_SERIES.out.cell_mask.map { m, f -> [m.patient_id, f] }
>     .join(EXTRACT_MASK_SERIES.out.nuclei_mask.map { m, f -> [m.patient_id, f] }, by: 0)
>
> def ch_prior_post = ch_prior_rows
>     .map { pid, merged_csv, _cm, pyramid -> [pid, merged_csv, pyramid] }
>     .join(ch_masks, by: 0)
>     .map { pid, merged_csv, pyramid, cell_mask, nuclei_mask ->
>         [pid, merged_csv, cell_mask, nuclei_mask, pyramid] }
> ```
>
> Fast-fail validation (your requirement) lives in two places: `EXTRACT_MASK_SERIES`
> errors clearly and early when the pyramid has no mask series (Task 7 Step 3),
> and `ParamUtils.validateAddCycle` already checks the checkpoint CSVs exist
> (Task 2). Add a `--dry_run` line logging that mask extraction will run. The
> `ch_prior_post` shape then feeds the existing `ch_prior_assets` join unchanged.

**Files:**
- Modify: `workflows/mirage.nf` (validation branch + prior-asset channels + `EXTRACT_MASK_SERIES` + ADD_CYCLE call)
- Modify: `conf/modules.config` (publishDir for ADD_CYCLE outputs)
- Create: `tests/subworkflows/add_cycle.nf.test`

**Interfaces:**
- Consumes: `ADD_CYCLE(ch_new_input, ch_prior_assets)` from Task 3; `ParamUtils.validateAddCycle` from Task 2; `loadInputChannel(csv, col, patient_counts, channel_counts)` (existing helper in `mirage.nf`).
- Produces: end-to-end `mode=add_cycle` execution.

- [ ] **Step 1: Add the routing branch in `workflows/mirage.nf`**

Immediately after the `ParamUtils.validateStart(params.start)` block (near line 72), insert a top-level mode branch that short-circuits the standard flow:

```groovy
    /* -------------------- MODE: ADD_CYCLE -------------------- */
    if (params.mode == 'add_cycle') {
        ParamUtils.validateAddCycle(params.prior_outdir)
        ParamUtils.validateSegMethod(params.seg_method)  // reuse mask; still validate quant options
        ParamUtils.validateCompartmentQuant(params.quantify_compartments, params.expanded_quantification)

        if (!params.input) error "mode='add_cycle' requires --input (the new cycle samplesheet)"
        CsvUtils.validateInputCSV(params.input, ParamUtils.requiredColumnsForStep('preprocessing'))
        CsvUtils.validateInputSemantics(params.input, 'preprocessing', params.allow_auto_reference)

        if (params.dry_run) {
            log.info "DRY RUN (add_cycle): validations passed for --input=${params.input}, --prior_outdir=${params.prior_outdir}"
            return
        }

        // New-cycle raw images -> [meta, file] (reuse existing loader + counts).
        def new_counts    = CsvUtils.countImagesPerPatient(params.input)
        def new_ch_counts = CsvUtils.countChannelsPerPatient(params.input)
        def ch_new_input  = loadInputChannel(params.input, 'path_to_file', new_counts, new_ch_counts)

        // Prior reusable assets from the previous run's checkpoint CSVs.
        // registered.csv: patient_id,registered_image,is_reference,channels
        def ch_prior_ref = Channel
            .fromPath("${params.prior_outdir}/csv/registered.csv", checkIfExists: true)
            .splitCsv(header: true)
            .filter { row -> row.is_reference?.toString().toLowerCase() == 'true' }
            .map { row ->
                def chans = (row.channels ?: '').split('\\|').collect { it.trim() }.findAll { it }
                [row.patient_id, chans, file(row.registered_image)]
            }
        // postprocessed.csv: patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid
        def ch_prior_post = Channel
            .fromPath("${params.prior_outdir}/csv/postprocessed.csv", checkIfExists: true)
            .splitCsv(header: true)
            .map { row ->
                def cell_mask   = file(row.cell_mask)
                // Nuclei mask sits beside the cell mask (SEGMENT emits both with the same stem).
                def nuclei_mask = file(row.cell_mask.replace('_cell_mask', '_nuclei_mask'))
                [row.patient_id, file(row.merged_csv), cell_mask, nuclei_mask, file(row.pyramid)]
            }

        // Join into a single per-patient asset tuple.
        def ch_prior_assets = ch_prior_ref
            .join(ch_prior_post, by: 0)
            .map { pid, ref_channels, ref_image, merged_csv, cell_mask, nuclei_mask, pyramid ->
                [pid, ref_channels, ref_image, merged_csv, cell_mask, nuclei_mask, pyramid]
            }

        ADD_CYCLE(ch_new_input, ch_prior_assets)
        return   // do NOT fall through to the standard start/stop flow
    }
```

Add the include near the other subworkflow includes (top of `mirage.nf`, after line 9):

```groovy
include { ADD_CYCLE           } from '../subworkflows/local/add_cycle'
```

- [ ] **Step 2: Add publishDir for the new mode's outputs**

In `conf/modules.config`, the ADD_CYCLE run reuses `EXPORT_GEOJSON`, `MERGE_AND_PYRAMID`, `MERGE_QUANT_CSVS` — which already publish to `${params.outdir}/${meta.patient_id}/{geojson,pyramid,quantification}`. Because `mode=add_cycle` is run with a **fresh `--outdir`** (locked design decision 4), no path change is required; the combined outputs land in the new outdir without clobbering the prior run. Add a comment marker so this is discoverable:

```groovy
    // NOTE: mode=add_cycle reuses EXPORT_GEOJSON / MERGE_AND_PYRAMID / MERGE_QUANT_CSVS
    // publishDir rules below. Run add_cycle with a fresh --outdir; combined outputs
    // (all cycles) are written there, leaving the prior run's outdir intact as the
    // --prior_outdir checkpoint source.
```
Place it directly above the `withName: 'MERGE_AND_PYRAMID'` block.

- [ ] **Step 3: Write the nf-test stub**

Create `tests/subworkflows/add_cycle.nf.test`:

```groovy
nextflow_workflow {

    name "Test ADD_CYCLE subworkflow (stub)"
    script "subworkflows/local/add_cycle.nf"
    workflow "ADD_CYCLE"

    test("folds a new cycle: emits combined geojson, merged csv, and pyramid") {
        options "-stub"

        when {
            workflow {
                """
                // New cycle: one non-reference slide with DAPI + a new marker.
                input[0] = Channel.of([
                    [ id: 'P001_cycle2', patient_id: 'P001', is_reference: false,
                      channels: ['DAPI','CD8'], images_count: 1, channels_count: 2 ],
                    file('tests/testdata/P001_image.tiff')
                ])
                // Prior assets: [patient_id, ref_channels, ref_image, base_merged_csv, cell_mask, nuclei_mask, pyramid]
                input[1] = Channel.of([
                    'P001', ['DAPI','PANCK'],
                    file('tests/testdata/P001_image.tiff'),
                    file('tests/testdata/P001_merged_quant.csv'),
                    file('tests/testdata/P001_cell_mask.tif'),
                    file('tests/testdata/P001_nuclei_mask.tif'),
                    file('tests/testdata/P001_pyramid.ome.tiff')
                ])
                """
            }
            params {
                outdir = "$outputDir"
                mode = 'add_cycle'
                reg_qc = 1
                quantify_compartments = false
            }
        }

        then {
            assert workflow.success
            assert workflow.out.geojson
            assert workflow.out.merged_csv
            assert workflow.out.pyramid
        }
    }

    test("reg_qc=2 + expanded quantification: seg_qc emitted, compartment path runs") {
        options "-stub"

        when {
            workflow {
                """
                input[0] = Channel.of([
                    [ id: 'P001_cycle2', patient_id: 'P001', is_reference: false,
                      channels: ['DAPI','CD8'], images_count: 1, channels_count: 2 ],
                    file('tests/testdata/P001_image.tiff')
                ])
                input[1] = Channel.of([
                    'P001', ['DAPI','PANCK'],
                    file('tests/testdata/P001_image.tiff'),
                    file('tests/testdata/P001_merged_quant.csv'),
                    file('tests/testdata/P001_cell_mask.tif'),
                    file('tests/testdata/P001_nuclei_mask.tif'),
                    file('tests/testdata/P001_pyramid.ome.tiff')
                ])
                """
            }
            params {
                outdir = "$outputDir"
                mode = 'add_cycle'
                reg_qc = 2
                quantify_compartments = true
                expanded_quantification = true
            }
        }

        then {
            assert workflow.success
            assert workflow.out.geojson
            assert workflow.out.pyramid
            assert workflow.out.seg_qc   // reg_qc=2 -> WARP_SEG_QC metrics present
        }
    }
}
```

If the referenced testdata fixtures do not exist, create empty placeholders first:
```bash
touch tests/testdata/P001_merged_quant.csv tests/testdata/P001_cell_mask.tif \
      tests/testdata/P001_nuclei_mask.tif tests/testdata/P001_pyramid.ome.tiff
```
(`tests/testdata/P001_image.tiff` should already exist from `generate_complete_testdata.py`; regenerate if not: `python tests/testdata/generate_complete_testdata.py`.)

- [ ] **Step 4: Run the stub test**

Run: `nf-test test tests/subworkflows/add_cycle.nf.test --profile test,docker --verbose`
Expected: PASS — `workflow.success` true; `geojson`, `merged_csv`, `pyramid` outputs present. (Stub blocks in each module emit touch-files, so this exercises the wiring, not the science.)

- [ ] **Step 5: Regression — standard mode still stubs green**

Run: `nextflow run . -profile test,docker -stub --outdir results_smoke`
Expected: completes with no error (standard flow, `params.mode='standard'`, untouched).

- [ ] **Step 6: Commit**

```bash
git add workflows/mirage.nf conf/modules.config tests/subworkflows/add_cycle.nf.test tests/testdata
git commit -m ":sparkles: Route mode=add_cycle end-to-end with stub coverage"
```

---

## Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (Architecture section — document the mode)
- Create: `docs/add_cycle.md` (usage)

**Interfaces:** none (docs only).

- [ ] **Step 1: Document usage**

Create `docs/add_cycle.md`:

```markdown
# Incremental cyclic-IF: `mode=add_cycle`

Fold a NEW imaging cycle into an already-completed patient run, reusing the prior
reference, segmentation mask, and old-marker quantification.

## Prerequisites
A previous run completed through postprocessing, producing under its `--outdir`:
`csv/registered.csv`, `csv/postprocessed.csv`, and per-patient
`segmentation/`, `quantification/`, `pyramid/` outputs.

## Run
```bash
nextflow run . -profile <profile> \
  --mode add_cycle \
  --prior_outdir results_cycle1 \
  --input new_cycle.csv \
  --outdir results_cycle2
```
- `--input`: same schema as a preprocessing start (`patient_id,path_to_file,is_reference,channels`),
  one row per new-cycle slide, `is_reference=false`, `DAPI` present.
- `--prior_outdir`: the previous run's `--outdir`.
- `--outdir`: a FRESH directory; the complete combined outputs (all markers,
  all cycles) are written here. The prior outdir is left intact.

## What is reused vs recomputed
- Reused: reference image, `*_cell_mask.tif` (no SEGMENT), morphology/contours,
  prior marker columns.
- Recomputed: preprocess + register the new slide; quantify new markers; rebuild
  `cells.geojson` and the pyramid from the combined set.

## Marker collisions
A new-cycle marker that shares a name with a prior column overwrites it
(new cycle wins). `DAPI` is protected and never overwritten.

## Caveat
New-marker intensities are read through the cycle-1 mask, valid only if the new
cycle registers accurately. Check `--reg_qc 1` (DAPI overlay) QC per patient;
poor registration means the new markers for that patient are unreliable.

## Chaining cycles
Cycle N's `--outdir` becomes cycle N+1's `--prior_outdir`.
```

- [ ] **Step 2: Add a pointer in CLAUDE.md**

In `CLAUDE.md`, under **Architecture**, after the "Step-based execution" bullet, add:

```markdown
- **Incremental cyclic-IF**: `params.mode='add_cycle'` (with `--prior_outdir` +
  `--input`) bypasses the linear step gate to fold a new imaging cycle into a
  completed patient run — reuses the prior reference, segmentation mask, and
  old-marker quantification; recomputes only the new cycle's registration and
  markers. See `docs/add_cycle.md` and `subworkflows/local/add_cycle.nf`.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/add_cycle.md
git commit -m ":memo: Document mode=add_cycle incremental cyclic-IF"
```

---

## Task 6: Mask-series writer in the pyramid (+ MERGE_AND_PYRAMID wiring)

**Files:**
- Modify: `bin/merge_channels_pyramid.py` (add optional mask-series write)
- Modify: `modules/local/merge_and_pyramid.nf` (accept optional masks; pass flags)
- Modify: `subworkflows/local/postprocess.nf` (join masks into the pyramid input)
- Modify: `conf/modules.config` (`MERGE_AND_PYRAMID` memory calc + ext.args)
- Test: `tests/test_merge_channels_pyramid_masks.py` (create)

**Interfaces:**
- Consumes: `params.embed_masks` (Task 2), `params.quantify_compartments`, `params.expanded_quantification`.
- Produces: when enabled, `pyramid.ome.tiff` has a second series `Image:1` with
  channels `['cell_mask','nuclei_mask']`, `uint32`, single full-resolution.
  `write_pyramidal_ome_tiff(...)` gains keyword `mask_stack: np.ndarray | None`
  (shape `(2, H, W)`, dtype `uint32`) and `mask_names: list[str]`. `merge_channels(...)`
  gains `masks_dir: str | None`. CLI gains `--masks-dir <dir>` (contains
  `cell_mask.tif` + `nuclei_mask.tif` when the mask series should be written).

- [ ] **Step 1: Write the failing round-trip test**

Create `tests/test_merge_channels_pyramid_masks.py`:

```python
import importlib.util
from pathlib import Path

import numpy as np
import tifffile

_SPEC = importlib.util.spec_from_file_location(
    "merge_channels_pyramid",
    Path(__file__).resolve().parent.parent / "bin" / "merge_channels_pyramid.py",
)
mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp)


def test_second_series_holds_uint32_masks(tmp_path):
    intens = np.random.randint(0, 4000, size=(3, 128, 128), dtype=np.uint16)
    masks = np.stack([
        np.random.randint(0, 100000, size=(128, 128), dtype=np.uint32),  # cell (>65535)
        np.random.randint(0, 100000, size=(128, 128), dtype=np.uint32),  # nuclei
    ])
    out = tmp_path / "pyr.ome.tiff"
    mcp.write_pyramidal_ome_tiff(
        intens, str(out),
        channel_names=['DAPI', 'CD8', 'PANCK'],
        channel_colors=[(0, 0, 255), (255, 0, 0), (0, 255, 0)],
        mask_stack=masks, mask_names=['cell_mask', 'nuclei_mask'],
        pyramid_resolutions=3,
    )
    with tifffile.TiffFile(out) as tif:
        assert len(tif.series) == 2
        assert tif.series[0].dtype == np.uint16       # intensities untouched
        s1 = tif.series[1].asarray()
        assert s1.dtype == np.uint32 and s1.shape == (2, 128, 128)
        np.testing.assert_array_equal(s1, masks)       # labels preserved exactly


def test_no_mask_stack_writes_single_series(tmp_path):
    intens = np.random.randint(0, 4000, size=(2, 64, 64), dtype=np.uint16)
    out = tmp_path / "pyr_nomask.ome.tiff"
    mcp.write_pyramidal_ome_tiff(
        intens, str(out),
        channel_names=['DAPI', 'CD8'],
        channel_colors=[(0, 0, 255), (255, 0, 0)],
        mask_stack=None, mask_names=None,
        pyramid_resolutions=3,
    )
    with tifffile.TiffFile(out) as tif:
        assert len(tif.series) == 1                    # unchanged behavior
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_merge_channels_pyramid_masks.py -v`
Expected: FAIL — `write_pyramidal_ome_tiff` has no `mask_stack`/`mask_names` kwargs yet.

- [ ] **Step 3: Implement the mask series**

In `bin/merge_channels_pyramid.py`, add `mask_stack: Optional[np.ndarray] = None`
and `mask_names: Optional[List[str]] = None` to `write_pyramidal_ome_tiff`. After
the intensity series + pyramid levels are written, inside the same
`with tifffile.TiffWriter(...) as tif:` block, append the mask series as a NEW
top-level image (a fresh `tif.write` with its own metadata creates a second OME
series; do NOT pass `subfiletype`/`subifds` — single resolution, labels must not
be averaged):

```python
        if mask_stack is not None:
            if mask_stack.dtype != np.uint32:
                mask_stack = mask_stack.astype(np.uint32)
            log(f"  Writing mask series (Image:1): {mask_stack.shape} uint32")
            tif.write(
                mask_stack,
                metadata={'axes': 'CYX', 'Channel': {'Name': mask_names or ['cell_mask', 'nuclei_mask']}},
                tile=(tile_size, tile_size),
                compression=compression,
                photometric='minisblack',
                resolutionunit='CENTIMETER',
            )
```

Add `--masks-dir` handling in `merge_channels(...)` and `main()`: if `masks_dir`
is given and contains `cell_mask.tif` + `nuclei_mask.tif`, read them (via
`_read_channel_file`), stack `(2, H, W)` uint32, and pass as `mask_stack`. Guard
that the mask H×W equals the intensity H×W (else raise `ValueError` with both
shapes logged — a fast, clear failure).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_merge_channels_pyramid_masks.py -v`
Expected: PASS (2/2).

- [ ] **Step 5: Wire MERGE_AND_PYRAMID to receive masks**

In `modules/local/merge_and_pyramid.nf`, change the input to stage an optional
mask directory alongside the channels:

```groovy
    input:
    tuple val(meta), path(split_channels, stageAs: 'channels/*'), path(mask_files, stageAs: 'masks/*')
```

In the script, add `--masks-dir masks` to the `merge_channels_pyramid.py` call
**only** when masks were staged and the toggles are on:

```groovy
    def emit_masks = params.embed_masks && params.quantify_compartments && params.expanded_quantification
    def masks_arg  = emit_masks ? "--masks-dir masks" : ""
```
(Place `${masks_arg}` in the command. When `mask_files` is an empty list Nextflow
stages no `masks/` dir and `masks_arg` is empty — intensity-only pyramid.)

Update the `stub:` block to still `touch pyramid.ome.tiff`. Update
`conf/modules.config` `MERGE_AND_PYRAMID` memory closure that references
`seg_mask.size()` — it now references `mask_files` (a list); guard for empty:
`def mask_gb = (mask_files ? mask_files.collect { it.size() }.sum() : 0) >> 30`.

- [ ] **Step 6: Wire postprocess.nf to pass the masks**

In `subworkflows/local/postprocess.nf`, where `MERGE_AND_PYRAMID(ch_for_pyramid_merge)`
is called (~line 269), join the per-patient cell+nuclei masks into the input.
The masks are `ch_cell_mask`/`ch_nuclei_mask` (both `[meta, mask]`). Only attach
them when the toggles are on; otherwise pass an empty list:

```groovy
    def emit_masks = params.embed_masks && params.quantify_compartments && params.expanded_quantification
    ch_pyramid_in = emit_masks
        ? ch_split_grouped
            .map { meta, tiffs -> [meta.patient_id, meta, tiffs] }
            .join(ch_cell_mask.map { m, f -> [m.patient_id, f] }, by: 0)
            .join(ch_nuclei_mask.map { m, f -> [m.patient_id, f] }, by: 0)
            .map { _pid, meta, tiffs, cm, nm -> [meta, tiffs, [cm, nm]] }
        : ch_split_grouped.map { meta, tiffs -> [meta, tiffs, []] }
    MERGE_AND_PYRAMID(ch_pyramid_in)
```

- [ ] **Step 7: Stub + regression**

Run: `nextflow run . -profile test,docker -stub --outdir results_smoke6` — expect success (masks empty in the default test profile → intensity-only path).
Run: `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration` — expect green.

- [ ] **Step 8: REQUIRED real-read verification (non-stub)**

This is the risk gate from the spec. With real test data (`python tests/testdata/generate_complete_testdata.py`), run a real (non-stub) postprocessing with `--quantify_compartments --expanded_quantification --embed_masks true`, then confirm with `python -c`:
```python
import tifffile, numpy as np
t = tifffile.TiffFile('<patient>/pyramid/pyramid.ome.tiff')
assert len(t.series) == 2 and t.series[1].asarray().dtype == np.uint32
print('series0', t.series[0].shape, t.series[0].dtype, 'pyramidal', t.series[0].is_pyramidal)
```
Report the output. If a QuPath/Bio-Formats reader is available, open the file and confirm `Image:0` renders as a normal multi-channel image. If verification fails, report BLOCKED — do not mark complete.

- [ ] **Step 9: Commit**

```bash
git add bin/merge_channels_pyramid.py modules/local/merge_and_pyramid.nf \
        subworkflows/local/postprocess.nf conf/modules.config \
        tests/test_merge_channels_pyramid_masks.py
git commit -m ":sparkles: Embed segmentation masks as a second pyramid series (embed_masks)"
```

---

## Task 7: Mask-series reader + fast-fail validation

**Files:**
- Create: `bin/extract_mask_series.py` (read `Image:1` → `cell_mask.tif`, `nuclei_mask.tif`)
- Create: `modules/local/extract_mask_series.nf` (`EXTRACT_MASK_SERIES` process)
- Test: `tests/test_extract_mask_series.py` (create)

**Interfaces:**
- Consumes: a pyramid OME-TIFF written by Task 6.
- Produces: `EXTRACT_MASK_SERIES([meta, pyramid]) -> out.cell_mask: [meta, cell_mask.tif]`,
  `out.nuclei_mask: [meta, nuclei_mask.tif]`. Fast-fails (non-zero exit, clear
  message) when the pyramid has no second series or the series is not a `uint32`
  2-channel mask. Task 4 uses this to source masks for `ADD_CYCLE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract_mask_series.py`:

```python
import importlib.util, subprocess, sys
from pathlib import Path

import numpy as np
import tifffile

BIN = Path(__file__).resolve().parent.parent / "bin"
_SPEC = importlib.util.spec_from_file_location("merge_channels_pyramid", BIN / "merge_channels_pyramid.py")
mcp = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(mcp)


def _pyramid_with_masks(path):
    intens = np.random.randint(0, 4000, size=(2, 96, 96), dtype=np.uint16)
    masks = np.stack([np.arange(96*96, dtype=np.uint32).reshape(96, 96),
                      np.arange(96*96, dtype=np.uint32).reshape(96, 96)])
    mcp.write_pyramidal_ome_tiff(intens, str(path), channel_names=['DAPI','CD8'],
        channel_colors=[(0,0,255),(255,0,0)], mask_stack=masks,
        mask_names=['cell_mask','nuclei_mask'], pyramid_resolutions=2)
    return masks


def test_extract_recovers_masks(tmp_path):
    masks = _pyramid_with_masks(tmp_path / "p.ome.tiff")
    subprocess.run([sys.executable, str(BIN / "extract_mask_series.py"),
                    "--pyramid", str(tmp_path / "p.ome.tiff"), "--outdir", str(tmp_path)], check=True)
    np.testing.assert_array_equal(tifffile.imread(tmp_path / "cell_mask.tif"), masks[0])
    np.testing.assert_array_equal(tifffile.imread(tmp_path / "nuclei_mask.tif"), masks[1])


def test_missing_series_fails_fast(tmp_path):
    intens = np.random.randint(0, 4000, size=(2, 64, 64), dtype=np.uint16)
    mcp.write_pyramidal_ome_tiff(intens, str(tmp_path / "n.ome.tiff"), channel_names=['DAPI','CD8'],
        channel_colors=[(0,0,255),(255,0,0)], mask_stack=None, mask_names=None, pyramid_resolutions=2)
    r = subprocess.run([sys.executable, str(BIN / "extract_mask_series.py"),
                        "--pyramid", str(tmp_path / "n.ome.tiff"), "--outdir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "no mask series" in (r.stderr + r.stdout).lower()
```

- [ ] **Step 2: Verify failure** — `pytest tests/test_extract_mask_series.py -v` → FAIL (no script yet).

- [ ] **Step 3: Implement `bin/extract_mask_series.py`**

```python
#!/usr/bin/env python3
"""Extract the mask series (Image:1) from a mask-carrying pyramid OME-TIFF.

Writes cell_mask.tif and nuclei_mask.tif (uint32). Fast-fails with a clear
message when the pyramid has no second series or it is not a 2-channel uint32
mask series (the incremental cyclic-IF mode relies on this).
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

import numpy as np
import tifffile


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pyramid", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    args = ap.parse_args()

    with tifffile.TiffFile(args.pyramid) as tif:
        if len(tif.series) < 2:
            sys.exit(f"ERROR: {args.pyramid} has no mask series (found {len(tif.series)} series). "
                     f"The prior run must set embed_masks=true with expanded compartment quantification.")
        masks = tif.series[1].asarray()
    if masks.ndim != 3 or masks.shape[0] != 2:
        sys.exit(f"ERROR: mask series has shape {masks.shape}; expected (2, H, W) [cell, nuclei].")
    if masks.dtype != np.uint32:
        masks = masks.astype(np.uint32)

    args.outdir.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(args.outdir / "cell_mask.tif", masks[0])
    tifffile.imwrite(args.outdir / "nuclei_mask.tif", masks[1])
    print(f"Extracted cell_mask + nuclei_mask from {args.pyramid} (series 1, {masks.shape[1]}x{masks.shape[2]})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify pass** — `pytest tests/test_extract_mask_series.py -v` → PASS (2/2).

- [ ] **Step 5: Make the script executable (name-invoked from Nextflow)**

```bash
git update-index --add --chmod=+x bin/extract_mask_series.py
git ls-files -s bin/extract_mask_series.py   # expect 100755
```

- [ ] **Step 6: Create the `EXTRACT_MASK_SERIES` process**

Create `modules/local/extract_mask_series.nf` following the repo template (tag
`${meta.patient_id}`, label `process_low`, container
`bolt3x/attend_image_analysis:merge`, `versions.yml`, a `stub:` that `touch`es
`cell_mask.tif`/`nuclei_mask.tif`). Input `tuple val(meta), path(pyramid)`;
outputs `cell_mask`/`nuclei_mask` tuples. Script calls
`extract_mask_series.py --pyramid ${pyramid} --outdir .`.

- [ ] **Step 7: Commit**

```bash
git add bin/extract_mask_series.py modules/local/extract_mask_series.nf tests/test_extract_mask_series.py
git commit -m ":sparkles: Extract mask series from pyramid for incremental reuse (fast-fail)"
```

---

## Self-Review

**Spec coverage:**
- Cycle model (true cyclic IF) → Task 3 reuses mask; QC in Task 3 Step 3. ✓
- Self-contained subworkflow → Task 3. ✓
- Preprocessing the new cycle → Task 3 Step 1 (`PREPROCESSING`). ✓
- Fresh outdir output policy → Task 4 Step 2 note + Task 5 docs. ✓
- Marker collision (new-wins-except-DAPI) → Task 1. ✓
- Registration-drift QC on by default → Task 3 Step 1 (`reg_qc` default 1). ✓
- Register new cycle to frozen reference (2-node VALIS graph, discard ref) → Task 3 Step 2. ✓
- `reg_qc=2` seg-overlap QC (SEG_QC_GEOJSON → WARP_SEG_QC via registrar pickle) → Task 3 Step 3. ✓
- Reuse mask → contours (EXTRACT_CELL_PROPERTIES) → Task 3 Step 4. ✓
- Expanded/compartment quantification (EXTRACT_NUCLEI_PROPERTIES + nuclei mask into QUANTIFY + nucleus contours into EXPORT_GEOJSON; `--expanded` via ext.args) → Task 3 Steps 4/6/8. ✓
- Quantify new markers vs existing mask → Task 3 Step 6. ✓
- Merge onto prior merged table (base) → Task 1 + Task 3 Step 7. ✓
- Regenerate geojson wholesale → Task 3 Step 8. ✓
- Rebuild pyramid by re-splitting prior pyramid + new channels → Task 3 Step 9. ✓
- Param surface + validation (`--mode`, `--prior_outdir`) → Task 2 + Task 4 Step 1. ✓
- Prior-asset channels from checkpoint CSVs → Task 4 Step 1. ✓

**`reg_qc=2` verification note:** the seg-overlap path relies on VALIS slide names matching between `SEG_QC_GEOJSON` (which strips `.ome.tif[f]`/`.tif[f]` from the input filename) and the registrar `slide_dict` keys built from the same files fed to `VALIS_ADAPTER`. This holds because ADD_CYCLE feeds the prior reference image and the preprocessed new-cycle image into both VALIS and `SEG_QC_GEOJSON`. The classic registrar pickle is available because ADD_CYCLE always uses `VALIS_ADAPTER` (never the distributed path). Confirm on the first real (non-stub) run that `WARP_SEG_QC` finds both slide keys.

**Nuclei-mask provenance for compartments:** `EXTRACT_NUCLEI_PROPERTIES` and compartment `QUANTIFY` consume the reused `nuclei_mask`, derived from the checkpoint `cell_mask` path via `_cell_mask`→`_nuclei_mask` (Task 4 Step 1). Confirm `SEGMENT` emits both masks with that shared stem (`segment.nf:35-36`) so the derived path resolves.

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `merge_intensities(...protected_cols)` defined in Task 1 and called in Task 3 via `MERGE_QUANT_CSVS` (unchanged process, base fed in the `--morphology` slot). `ch_prior_assets` 7-tuple shape identical in Task 3 `take` and Task 4 construction. `ADD_CYCLE(ch_new_input, ch_prior_assets)` signature matches between Task 3 and Task 4. `SPLIT_CHANNELS` third input `is_reference` boolean matches module signature. ✓

**Nuclei-mask note:** the plan derives `nuclei_mask` from the `cell_mask` path (`_cell_mask`→`_nuclei_mask`); with `quantify_compartments=false` (v1 default) `QUANTIFY` ignores it. Compartment quantification in add_cycle mode is a documented follow-up (needs verified nuclei-mask provenance).
