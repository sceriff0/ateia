# Design: `ADD_CYCLE` — Incremental Cyclic-IF Subworkflow

**Date:** 2026-07-20
**Status:** Approved design, pending implementation plan
**Author:** brainstormed with Claude Code

## Problem

A patient has already been run through the full Mirage pipeline
(`preprocessing → registration → postprocessing`) and has all outputs: the
registered reference, the segmentation masks, the merged quantification table,
`cells.geojson`, and the multi-channel pyramid OME-TIFF.

The user now acquires a **new imaging cycle** of the **same physical tissue
section** (true cyclic IF — e.g. CyCIF/CODEX/mIF: strip, re-stain, re-image),
containing a fresh `DAPI` anchor plus new markers (`DAPI + MARKER1 + …`). They
want to fold the new markers into the existing patient result **without
reprocessing the whole patient** — reusing the prior reference, segmentation,
and old-marker quantification to save time, and obtaining a single **complete**
output (all markers across all cycles in one geojson and one pyramid).

## Feasibility Summary

Feasible. The pipeline's architecture separates registration from
segmentation/quantification cleanly, and the segmentation mask is a reusable,
standalone label TIFF in the reference coordinate frame.

| Asset | Fate | Evidence |
|---|---|---|
| Reference image + prior registered slides | Reuse as-is | Fixed reference coord frame; passthrough unregistered (`registration.nf:157`) |
| Segmentation masks (`*_cell_mask.tif`, `*_nuclei_mask.tif`) | **Reuse as-is** | `SEGMENT` runs only on reference (`postprocess.nf:53-60`); published label TIFFs (`segment.nf:35-36`) |
| Morphology + cell contours | Reuse | Mask-derived (`EXTRACT_CELL_PROPERTIES`) |
| Existing quant table (`merged_csv`) | Reuse as merge base + append columns | Left-join on cell `label` (`bin/merge_quant_csvs.py`) |
| **Registration of the new cycle's slide** | **Recompute (unavoidable)** | VALIS has no add-one-slide-to-frozen-reference API (`bin/register.py:430-440`) |
| New markers' quantification | Recompute (cheap) vs existing mask | `QUANTIFY = (channel_tiff, mask) → label-keyed CSV` (`quantify.nf:16-17`) |
| `cells.geojson` | Regenerate wholesale from combined table (cheap) | No append path; auto-discovers marker cols (`bin/export_geojson.py`) |
| Final pyramid | Regenerate wholesale from combined channels (moderate) | Globs a channel dir (`bin/merge_channels_pyramid.py:507-513`) |

**The only unavoidable heavy cost is registering the new cycle.** All spatial
downstream work rides on the shared coordinate frame. Skipped entirely:
`SEGMENT` (expensive StarDist/CellSAM), re-registration of old cycles,
re-quantification of old markers.

## Scope Decisions (locked)

1. **Cycle model:** true cyclic IF — same physical section re-imaged. Mask reuse
   is scientifically valid; cross-cycle registration is the only alignment risk.
2. **Integration:** a new self-contained subworkflow
   `subworkflows/local/add_cycle.nf`, invoked by a new mode, reusing existing
   modules. The existing linear 3-step flow is untouched.
3. **Preprocessing:** the new cycle **is** run through `PREPROCESSING` first
   (mirrors cycle-1 illumination/prep).
4. **Output policy:** the incremental run writes a **fresh `--outdir`**
   (e.g. `results_cycle2/`) holding the complete combined outputs. Cycle-1
   outputs stay untouched as the checkpoint/input source.
5. **Marker-name collision:** **new cycle wins** (its column overwrites a
   colliding prior column) — **except `DAPI`, which is protected** and always
   remains the reference anchor. (Largely automatic: `SPLIT_CHANNELS` already
   drops DAPI from non-reference images, `split_channels.nf:29`, so a new-cycle
   DAPI never becomes a marker column or pyramid band. The overwrite logic must
   explicitly exclude DAPI as a guard.)
6. **Registration-drift QC:** ON by default (reuses `GENERATE_REGISTRATION_QC`
   and optionally `WARP_SEG_QC`), non-gating like all current QC.

## Architecture

### New file
`subworkflows/local/add_cycle.nf` — subworkflow `ADD_CYCLE`.

### Inputs
1. **Prior-run checkpoint(s)**, produced by the earlier full run:
   - `csv/registered.csv` (`registration.nf:362-367`) → reference image path +
     `is_reference` per patient.
   - `csv/postprocessed.csv` (`postprocess.nf:318-327`) → `cell_mask`,
     `merged_csv`, `pyramid` per patient.
   Consumed via a new param, e.g. `--prior_checkpoint <dir-or-csv>`, keyed by
   `patient_id`.
2. **New-cycle input CSV** (`--input`), same schema as a normal preprocessing
   start: `patient_id, path_to_file, is_reference, channels`, `DAPI` present.
   Rows describe the new cycle's slide(s). `is_reference` here is **false** for
   the new cycle — the frozen cycle-1 reference is the fixed frame.

### Data flow

```
new-cycle raw ─► PREPROCESSING ─► REGISTER (new slide → frozen cycle-1 reference)
                                       │
                                       ├─► GENERATE_REGISTRATION_QC (+ WARP_SEG_QC)   [drift check, non-gating]
                                       │
                     ┌─────────────────┴─────────────► SPLIT_CHANNELS (new registered)
  existing cell_mask.tif (REUSED, no SEGMENT) ──────────────────┐    │
                                                                 ▼    ▼
                                                 QUANTIFY(new channel × existing mask)
                                                                 │
    existing merged_csv (BASE) ─► MERGE_QUANT_CSVS (left-join new marker cols on `label`)
                                                                 │
                                                                 ├─► EXPORT_GEOJSON (combined table → complete cells.geojson)
                                                                 │
  existing pyramid ─► SPLIT_CHANNELS (recover old channels) ─┐   │
  new-cycle split channels ──────────────────────────────────┴─► MERGE_AND_PYRAMID (complete pyramid)
```

### Registration mechanics
The new slide registers as a **2-node VALIS graph** `{frozen reference, new
cycle}` with the reference marked `is_reference=true` — exactly the
`[patient_id, ref_item, items]` structure `VALIS_ADAPTER` already consumes
(`registration.nf:144`, `adapters/valis_adapter.nf:30-40`). The reference is
passed through unregistered and **discarded** (we already have it); only the new
cycle's registered output is kept. The new cycle's own DAPI is auto-dropped by
`SPLIT_CHANNELS`, so there is no duplicate DAPI.

### Pyramid rebuild
`SPLIT_CHANNELS` has **no `publishDir`** — cycle-1 single-channel TIFFs are not
persisted. So the combined pyramid is rebuilt by:
1. `SPLIT_CHANNELS` on the **existing published pyramid** (fed with
   `is_reference=true` so its reference DAPI + all old markers are recovered as
   single-channel TIFFs).
2. `SPLIT_CHANNELS` on the **new registered cycle** (markers only).
3. `MERGE_AND_PYRAMID` over the union → complete pyramid.

### Quant-table merge
The existing `merged_csv` already contains `label` + morphology + all old marker
columns. It becomes the **base table** for `MERGE_QUANT_CSVS`; new marker CSVs
left-join onto it by `label`. This is the pipeline's existing join semantics
(`bin/merge_quant_csvs.py`) with the base swapped from the morphology table to
the prior merged table. Collision rule: new column overwrites same-named prior
column, except `DAPI` is never overwritten.

### GeoJSON rebuild
`EXPORT_GEOJSON` auto-discovers marker columns from the combined table and
regenerates the full FeatureCollection, reusing the prior contours (mask-derived,
unchanged). Output honors the existing FlowPath measurement-key contract.

## New Code Inventory

| Area | Change | Kind |
|---|---|---|
| `subworkflows/local/add_cycle.nf` | New subworkflow orchestration | New |
| `workflows/mirage.nf` | Branch on `params.mode == 'add_cycle'` → call `ADD_CYCLE`, bypassing `STEP_ORDER` | New routing |
| `lib/CsvUtils.groovy` | Parse prior checkpoint CSVs; join prior assets to new-cycle rows by `patient_id` | New helper |
| `lib/ParamUtils.groovy` | Validate the new mode + required params/columns | New helper |
| `bin/merge_quant_csvs.py` | Accept an existing merged CSV as the join base; DAPI-protected overwrite of colliding columns | **Only Python change** |
| `conf/modules.config` | publishDir for combined outputs (new outdir) | Config |
| `tests/` | nf-test for `ADD_CYCLE` (stub + real); Python unit test for the merge-base change | Tests |
| docs / `CLAUDE.md` | Document the mode | Docs |

## Risks & Mitigations

1. **Cross-cycle registration drift** → mask misalignment → biased new-marker
   intensities. *Mitigation:* drift QC ON by default (DAPI overlay `reg_qc=1`;
   optional seg-overlap Dice/IoU `reg_qc=2`). Non-gating — surfaces evidence
   without blocking.
2. **Marker-name collision.** *Mitigation:* new-cycle-wins overwrite with DAPI
   protection (locked decision 5).
3. **Patient-key mismatch** between prior checkpoint and new-cycle CSV.
   *Mitigation:* validation fails fast if a new-cycle `patient_id` has no prior
   checkpoint entry.
4. **Reference availability.** The reference image must be the original
   reference pixels. `registered.csv` points at the published reference
   passthrough — available. Validation checks the file exists.
5. **Stale contours vs. combined table.** Contours are mask-derived and the mask
   is unchanged, so cell `label`s stay valid; no risk as long as the same mask
   is reused (enforced by construction).

## Amendment (2026-07-20): mask-carrying pyramid

Design extension decided after the initial spec. Motivation: make the pyramid a
richer, more self-contained artifact and let the incremental run source the
reusable masks from the pyramid itself.

### `embed_masks` parameter (new; default `true`)
`MERGE_AND_PYRAMID` writes the segmentation masks as a **second image series**
inside the pyramid OME-TIFF — never as extra channels. Rationale:
- An OME-TIFF `Pixels` block has one dtype for all channels; a `uint32` label
  mask would force the whole (intensity) stack to `uint32`, breaking QuPath
  (`postprocess.nf:260-263`, `merge_channels_pyramid.py:180`).
- Pyramid downsampling is **mean over 2×2 blocks**
  (`merge_channels_pyramid.py:186-188`); averaging categorical label IDs
  corrupts them. So the mask series is written **single full-resolution**
  (no averaged pyramid), preserving labels.

Layout: `Image:0` = intensity channels (`uint16`, pyramidal, QuPath opens this);
`Image:1` = `cell_mask` + `nuclei_mask` (`uint32`, 2 channels, single-res).

Gating: the mask series is written when
`embed_masks && quantify_compartments && expanded_quantification`. When
`embed_masks=false` (or not expanded/compartment) → today's intensity-only
pyramid, byte-compatible with current behavior. This changes the **standard**
postprocessing wiring too: `MERGE_AND_PYRAMID` now also receives the cell +
nuclei masks (currently it receives only the split channels).

### Incremental run sourcing (revised)
- **Masks:** extracted from the prior pyramid's `Image:1` series at run start.
  Fallback to the separate published `*_cell_mask.tif`/`*_nuclei_mask.tif` from
  the checkpoint **only if present**.
- **Base table:** the prior `merged_quant.csv` (via `--prior_outdir`), with new
  markers left-joined by the collision-aware merge (new wins, DAPI protected).
  Old markers are **not** re-quantified.
- **Input model:** stays the checkpoint path (`--prior_outdir` + `--input`); a
  "pyramid-only" input is not viable because the pyramid cannot carry the base
  measurement table.

### Fast-fail validation (required; runs in the validation phase, visible under `--dry_run`)
- `add_cycle` with `quantify_compartments`/`expanded_quantification` on but the
  prior pyramid has **no mask series** and no separate mask files → hard error
  naming the file and instructing the user the prior run needs `embed_masks=true`.
- Mask series malformed (wrong channel count, not `uint32`) → hard error with
  the detected shape/dtype logged.

### Verification gate (non-negotiable)
Writing + reading a multi-series pyramidal OME-TIFF (pyramidal `uint16`
intensities + flat `uint32` mask series in one file) must be verified with a
real (non-stub) `tifffile` round-trip **and** an actual QuPath/Bio-Formats read
of `Image:0`, not only a unit test.

## Non-Goals (YAGNI)

- True in-place append to `cells.geojson` or the pyramid (both are cheap to
  regenerate wholesale; an append path would be net-new, error-prone code).
- An incremental "add one slide to a frozen VALIS registrar" primitive (VALIS
  offers none; per-cycle registration is inherent and acceptable).
- Multi-cycle history management under one outdir (locked decision 4 uses a
  fresh outdir per run; running N cycles chains naturally: cycle-N's outdir
  becomes cycle-(N+1)'s checkpoint source).

## Open Implementation Questions (for the plan)

- Exact param surface: `--mode add_cycle` vs `--start add_cycle`; single
  `--prior_checkpoint` dir vs explicit `--prior_registered_csv` /
  `--prior_postprocessed_csv`.
- Whether `WARP_SEG_QC` (`reg_qc=2`) needs the registrar pickle path for the new
  2-node graph (classic VALIS only) — confirm it is produced in this mode.
