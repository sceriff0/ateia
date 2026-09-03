> **SUPERSEDED — historical record, 2026-09-02 (MIRAGE v1.0.0, release plan 13).**
> This document plans the `paper_data/` deliverable and mentions, as an input, the
> ANHIR/ACROBAT landmark harness (`benchmarks/registration_eval/`) and the synthetic
> ground-truth rung that replaced it (`benchmarks/stare_bench/`); both were deleted and
> exist on no branch. `benchmarks/README.md` section B is the removal record. Nothing
> below is runnable; it is kept because it is the dated record of a decision that was
> actually taken. Do not edit the body — a July plan rewritten to describe a September
> tree is a lie about its own date.

# Benchmark → method-paper DATA (post-main reconciliation)

**Date:** 2026-07-24
**Branch:** `bench/reconcile-main` (off `benchmarking`, merging `main`)
**Status:** design — awaiting review

## 1. Problem

The `benchmarking` branch split from `main` on 2026-06-30. Since then `main` is
113 commits ahead and `benchmarking` 76 ahead, and both edited the same
registration code (`bin/reg_*.py`, `modules/local/reg_*.nf`). Two things must
happen at once:

1. **Reconcile** `benchmarking` with everything on `main`.
2. **Re-aim** the benchmark at producing *tidy, documented DATA tables* (not
   plots) that a method paper can cite directly.

A structural conflict forces a decision: the `benchmarking` branch was built
largely to compare **classic vs distributed/tiled VALIS registration** (parity,
drift, RAM win). **`main` removed the entire tiled/distributed registration path
on 2026-07-24** (archived at tag `archive/tiled-valis-2026-07-24`). Registration
on `main` is now *classic `REGISTER` only*, so ~⅓ of the benchmark surface
measures code that no longer ships.

## 2. Decisions (confirmed with the author)

| # | Decision |
|---|----------|
| D1 | **Drop the distributed comparison.** Strip all distributed/tiled sweep grids and `reg_dist_*` params. Benchmark only the shipped classic pipeline. History + the archive tag preserve the old comparison. |
| D2 | **Invent no metrics.** Registration accuracy and reference-free segmentation quality are *already computed by `main`* (see §4). The benchmark harvests them; it does not define new ones. |
| D3 | **Registration-accuracy headline = co-headline** `displacement_um` (Δ vs the rigid anchor) **and** `dice_matched`. Everything else is stored too. |
| D4 | **Segmentation quality = CellSegmentationEvaluator (CSE)** `QualityScore` + its component metrics, reference-free. Inter-method agreement (already in the harness) is kept as a secondary stability signal. |
| D5 | **No auto-optimized config.** Emit the full parameter matrix; the author tunes. |
| D6 | **Merge rule:** main wins on all pipeline code (`bin/`, `modules/`, `subworkflows/`, `conf/`, `nextflow_schema.json`); the branch wins under `benchmarks/`. Exceptions are surfaced before finalizing. |

## 3. Reconciliation

1. Merge `main` into `bench/reconcile-main`. Resolve `bin/reg_*.py` /
   `modules/local/reg_*.nf` conflicts in favour of `main` (D6). `benchmarks/`
   merges cleanly — `main` never touches it.
2. Delete the distributed surface:
   - `benchmarks/configs/sweep.yaml`: remove `distributed_grid`,
     `distributed_tiling_grid`, and the classic-vs-distributed
     `registration_param_grid` (keep a classic-only version if the memory-mode ×
     micro-registration cross is still wanted at the baseline cell).
   - `benchmarks/build_run_plan.py`: remove the `distributed_grid` /
     `distributed_tiling_grid` expansion blocks and all `reg_dist_*` config keys.
   - `benchmarks/analysis/lib/compare_reg.py`: delete (whole file).
   - `benchmarks/analysis/make_figures.py`: remove the classic-vs-distributed
     section (`classic_vs_distributed_registration.csv`,
     `registration_drift.csv`) and the `reg_distributed_tiling` /
     `reg_dist_force_tiling` label columns.
   - `benchmarks/tests/test_compare_reg.py`: delete.
   - `benchmarks/README.md`, `docs/benchmarks.md`: strip distributed sections.
3. Switch the accuracy signal: drop `enable_feature_error` from the sweep
   baseline; add `reg_qc: 2` (already-shipped staged QC) and keep
   `skip_seg_quality_eval: false` (CSE default-on).

## 4. Where the metrics already come from (main)

**Registration accuracy — `reg_qc = 2`.** `bin/warp_seg_qc.py` +
`bin/utils/cell_pairs.py` segment DAPI nuclei on each slide, fix cell-to-cell
correspondence *once* at the rigid stage, then re-measure the same pairs after
each VALIS transform stage (`native → rigid → non_rigid → micro`). Emits
`<outdir>/<patient>/qc/registration/<patient>_<slide>_seg_qc.json` with, per
stage: `dice_matched`, `iou_mean/p10/p50/p90`, `frac_iou_ge_0.5`,
`displacement_px_p50/p90/max`, `displacement_um_p50/p90/max`, plus
`delta_vs_anchor` (stage minus rigid), `pair_fraction`, `n_pairs`. Docs:
`docs/registration_qc.md`. The Δ-vs-rigid displacement isolates registration
from segmentation noise; that is the number to quote alongside `dice_matched`.

**Segmentation quality — `SEG_QUALITY_EVAL`.** `modules/local/seg_quality_eval.nf`
runs the vendored CellSegmentationEvaluator (`bin/utils/cse/`) →
`<patient>_seg_eval.json` with a composite `QualityScore` and component metrics
(`NumberOfCellsPer100SquareMicrons`, `FractionOfForegroundOccupiedByCells`,
`FractionOfCellMaskInForeground`, `FractionOfMatchedCellsAndNuclei`,
`1/(AvgCVForegroundOutsideCells+1)`, silhouette, …). Reference-free. Gated by
`skip_seg_quality_eval` (default off = runs), `cse_pixel_size_um`, `segeval_tag`.

## 5. Deliverable: `benchmarks/paper_data/`

Every table is a tidy CSV with a sibling `<name>.dict.md` data dictionary
(column, unit, definition, source file). The R/ggplot + notebook code stays but
is demoted to *consumers* of these CSVs.

### `runs_master.csv` — one row per run
Config knobs + input dims (`target_px, width, height, n_channels,
n_register_images`) + per-stage `<stage>_peak_ram_gb`, `<stage>_wall_s`,
`cpu_hours`, `gpu_hours`, `status`, `varied_axis`. Built from the existing
`load_runs` / `measurements.csv` path (successful rows only; failures stay in
`measurements.csv`, lossless).

### Table A — `scaling_fits.csv` (resource laws)
Per `(stage, metric)`: fit `metric = a · input_bytes^b` → `a, exponent_b, R2, n,
ci_low, ci_high`. This is the renamed/paper-framed `resource_models.csv` from
`regress.models_to_frame`. The exponents are the paper's cost model.

### Table B — `registration_accuracy.csv` (harvested; new harvester)
One row per `(run_id, patient, moving, stage)`, from `*_seg_qc.json`:
`dice_matched, iou_mean, iou_p50, frac_iou_ge_0.5, displacement_um_p50,
displacement_um_p90, delta_disp_um_vs_rigid, delta_dice_vs_rigid, pair_fraction,
n_pairs`. New `harvest_registration_qc()` in `lib/quality.py` replaces
`harvest_registration_error()` (feature-distance JSON).

### Table B2 — `registration_valis_rtre.csv` (added 2026-07-24)
VALIS's *own* feature-based registration error — a **second, independent** accuracy signal.
`registrar.register()` reports an `error_df`; the REGISTER module publishes it to
`<patient>/registered/summary/*.csv` and it already renders in the final QC report
("Registration Accuracy (Valis rTRE)"). `harvest_valis_rtre()` reads those CSVs verbatim
(VALIS columns: `original_D, rigid_D, non_rigid_D`, rTRE variants, `n_matches`);
`valis_rtre_per_run()` medians the numeric columns (prefixed `valis_`) into `param_matrix`.
The paper reports both this and the segmentation-based Dice/displacement.

### Table C — `segmentation_eval.csv` (harvested; new harvester)
One row per `(run_id, patient)`, from `*_seg_eval.json`: `seg_method,
QualityScore` + the flattened CSE component metrics. New `harvest_seg_quality()`
in `lib/quality.py`. `segmentation_agreement.csv` (existing inter-method
instance-F1) is retained as the secondary stability signal.

### Table D — `param_matrix.csv` (author tunes)
`runs_master.csv` left-joined with Table B (`delta_disp_um_vs_rigid`,
`dice_matched` at `micro`/last stage) and Table C (`QualityScore`) per run, so
every knob's cost *and* quality sit in one wide table. `varied_axis` lets the
author slice scaling-grid rows (Table A) from OFAT rows (Table D).

## 6. Code changes (concrete)

| File | Change |
|------|--------|
| `benchmarks/configs/sweep.yaml` | Remove distributed grids; baseline `reg_qc: 2`, drop `enable_feature_error`, keep `skip_seg_quality_eval: false`. |
| `benchmarks/build_run_plan.py` | Remove distributed-grid expansion + `reg_dist_*` keys. |
| `benchmarks/run_sweep.sh` | Stop passing `reg_dist_*`; pass `reg_qc`, `cse_pixel_size_um` from the per-run config. |
| `benchmarks/analysis/lib/quality.py` | `harvest_registration_error` → `harvest_registration_qc` (reads `*_seg_qc.json`); add `harvest_seg_quality` (reads `*_seg_eval.json`). Keep `segmentation_agreement`, `run_cost_summary`. |
| `benchmarks/analysis/lib/compare_reg.py` | Delete. |
| `benchmarks/analysis/make_figures.py` | Drop distributed section; write the `paper_data/` table set (rename `resource_models.csv`→`scaling_fits.csv`; add `runs_master.csv`, `registration_accuracy.csv`, `segmentation_eval.csv`, `param_matrix.csv`) each with a `.dict.md`. |
| `benchmarks/analysis/make_tables.py` (new) | Thin entry point that emits `paper_data/` from a results root — the DATA-first CLI, independent of figure rendering. |
| `benchmarks/tests/` | Delete `test_compare_reg.py`; add `test_quality_harvest.py` (synthetic `*_seg_qc.json` / `*_seg_eval.json` fixtures) and a `paper_data` smoke test. |
| `benchmarks/README.md`, `docs/benchmarks.md` | Rewrite around the DATA deliverable + run recipe (§7). |

## 7. How to run it

**Local, no data — verify the harness:**
```bash
python -m pytest benchmarks/tests -q
```

**Cluster — the sweep (produces per-run pipeline outputs incl. the QC JSONs):**
```bash
# 1. Build the synthetic input matrix straight from the sweep
python benchmarks/generate_matrix.py \
    --source /path/to/your_source.ome.tif \
    --outdir bench_matrix \
    --sweep benchmarks/configs/sweep.yaml

# 2. Expand the sweep into a run plan (3 repeats/config)
python benchmarks/build_run_plan.py \
    --sweep benchmarks/configs/sweep.yaml \
    --out bench_run_plan.csv --repeats 3

# 3. Run the sweep (cluster; each run executes the pipeline with reg_qc=2 + CSE on).
#    Positional args: <run_plan> <matrix_manifest> <results_root>. Requires bash 4+.
bash benchmarks/run_sweep.sh bench_run_plan.csv bench_matrix/matrix_manifest.csv bench_runs
```

**Local — emit the paper DATA from the completed runs:**
```bash
python benchmarks/analysis/make_tables.py \
    --results-root bench_runs \
    --run-plan bench_run_plan.csv \
    --manifest bench_matrix/matrix_manifest.csv \
    --outdir benchmarks/paper_data
# -> benchmarks/paper_data/{runs_master,scaling_fits,registration_accuracy,
#    segmentation_eval,segmentation_agreement,param_matrix}.csv (+ .dict.md each)
```

A partial-results mode (analyze while the sweep is still running) is retained
from the branch: `make_tables.py` skips runs without a completed `out/` tree and
notes how many were skipped (no silent truncation).

## 8. Out of scope

- Reintroducing the distributed path or its comparison (archived; D1).
- Auto-deriving an optimized `modules.config` (D5 — author tunes from Table D).
- New segmentation/registration metrics (D2 — harvest only).
- Real landmark challenge datasets (ANHIR/ACROBAT) — not used; accuracy comes
  from the pipeline's own DAPI-nuclei staged QC.

## 9. Testing

- `benchmarks/tests` stays green (127 baseline; `test_compare_reg.py` removed,
  harvest tests added) on synthetic fixtures — no real data required.
- New harvesters tested against hand-written `*_seg_qc.json` / `*_seg_eval.json`
  fixtures matching the schemas in §4.
- `make_tables.py` smoke test asserts every `paper_data/*.csv` has its `.dict.md`
  and the documented columns.
