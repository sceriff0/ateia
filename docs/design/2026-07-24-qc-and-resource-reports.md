# Design: Comprehensive QC report + Computational-resources report

**Date:** 2026-07-24
**Status:** Approved design, pending implementation plan
**Scope:** Two deliverables — (A) overhaul the aggregated final QC report so it captures *all* QC plus optional/non-QC run context; (B) add a new standalone computational-resources report built from the size logs and Nextflow trace.

---

## 1. Motivation

The pipeline already produces a self-contained final QC report
(`bin/generate_qc_report.py`, wired via `modules/local/generate_qc_report.nf`
and assembled in `workflows/mirage.nf`). Two problems:

1. **The QC report silently drops QC artifacts the pipeline already computes**,
   and ignores non-QC run context that a publication-grade report needs.
2. **Rich resource data is produced but never turned into a report.** Per-task
   `*.size.csv` input-size logs are aggregated to `input_sizes.csv`, and
   Nextflow writes `.trace/trace.txt` with per-task `duration, realtime, %cpu,
   peak_rss, peak_vmem, rchar, wchar`. Nothing correlates *input size* with
   *resource usage* — the one view Nextflow's native `report.html` cannot give.

### 1.1 QC gap audit (what exists vs. what the report shows)

| QC artifact | Produced by | Current report |
|---|---|---|
| Preprocess PNGs | `GENERATE_PREPROCESS_QC` | shown |
| Registration overlays (`*_QC_RGB.{png,tif}`) | `GENERATE_REGISTRATION_QC` | shown |
| Valis rTRE summary CSV | valis adapter | shown |
| Feature-distance JSON | `ESTIMATE_FEATURE_DISTANCES` | shown (table) |
| **Feature-distance histograms** (`*_distance_histogram.png`) | `ESTIMATE_FEATURE_DISTANCES` | **DROPPED** — subworkflow never forwards `distance_plots` to the report |
| **Warp-seg QC metrics** (`*_seg_qc.json`) | `WARP_SEG_QC` | **DROPPED** — `REGISTRATION.out.seg_qc` is emitted but no consumer reads it |
| **Segmentation overlays** (`*_seg_overlay.png`) | `GENERATE_POSTPROCESSING_QC` | **DROPPED** — explicitly filtered out (`if "_seg_overlay" not in p.name`) |
| Postprocess intensity/morphology histograms | `GENERATE_POSTPROCESSING_QC` | shown |
| Segmentation-eval CSE metrics CSV | `MERGE_SEG_EVAL` | shown |
| **Software versions** (`collated_versions.yml`) | all processes | **DROPPED** — passed into the process, never read by the Python |

`CellSegmentationEvaluator` (CSE) is the current — and only — segmentation-eval
method (added recently: `SEG_QUALITY_EVAL` → `MERGE_SEG_EVAL`). It emits per-sample
JSON merged into `segmentation_metrics.csv` with an `id`, `QualityScore`, and a
dynamic set of `metric::submetric` columns. Already surfaced as a table; this
design promotes its headline `QualityScore` into the run summary.

---

## 2. Deliverable A — Overhauled QC report

### 2.1 New/changed sections (render order)

1. **Run summary card** (new, top of report)
   - Pipeline name + version (`workflow.manifest.name`, `.version`), run
     timestamp, run command / profile if available.
   - Mode: `standard` vs `add_cycle`; `start` → `stop` steps actually run.
   - Key parameters actually used: `registration_method`, `seg_method`,
     `quantify_compartments`, `expanded_quantification`, `pixel_size`,
     `cse_pixel_size_um`, relevant `skip_*` flags.
   - Headline metrics when available: mean/median CSE `QualityScore`,
     mean registration rTRE, mean feature-distance improvement %.

2. **Stage status strip** (new)
   - One badge per stage — Preprocessing / Registration / Segmentation+Quant —
     showing `ran` / `skipped` / `ran, no QC artifacts`. Derived from which
     input directories are non-empty (no new signalling needed).

3. **Sample manifest** (new)
   - Counts: patients, images, channels. Table of patient_id → image/channel
     counts. Sourced from the counts already computed in `workflows/mirage.nf`
     (`CsvUtils.countImagesPerPatient` / `countChannelsPerPatient`).

4. **Preprocessing QC** (unchanged) — image grid.

5. **Registration QC** (extended)
   - Overlay images (unchanged).
   - Valis rTRE table (unchanged).
   - Feature-distance table (unchanged) **+ NEW distance-histogram image grid**.
   - **NEW warp-seg QC table** rendered from `*_seg_qc.json`.

6. **Segmentation Overlays** (new dedicated section)
   - The `*_seg_overlay.png` images that were previously filtered out, shown in
     their own wide grid (they are the most-inspected visual QC).

7. **Postprocessing QC** (unchanged) — intensity/morphology histograms, still
   excluding seg overlays from *this* grid (they now have their own section).

8. **Segmentation Quality (CSE)** (unchanged) — metrics table.

9. **Software versions** (new) — table parsed from `collated_versions.yml`.

### 2.2 Data flow / wiring changes

- **`run_summary.json`** — written in `workflows/mirage.nf` from a Groovy map
  (`params` subset + `workflow` manifest + the pre-computed patient/channel
  counts) via `Channel.of(JsonOutput.toJson(map)).collectFile(name:
  'run_summary.json')`. No new process. Passed as a new input to
  `GENERATE_QC_REPORT`.
- **Distance histograms** — add a `distance_plots` emit to
  `subworkflows/local/registration.nf` (mix
  `ESTIMATE_FEATURE_DISTANCES.out.distance_plots`), forward to the report.
- **Warp-seg QC** — consume the already-emitted `REGISTRATION.out.seg_qc` in the
  final-report block of `workflows/mirage.nf`; pass as a new input.
- **Seg overlays** — **no wiring change**. Verified: `POSTPROCESSING.out.postprocess_qc`
  is `GENERATE_POSTPROCESSING_QC.out.qc.map { meta, pngs -> pngs }`, which already
  includes `*_seg_overlay.png` in the same staged `postprocess_qc/` dir as the
  histograms. They already reach the report; the change is Python-only: stop
  filtering them out of the postprocess grid and route them to the new
  Segmentation Overlays section (split by filename, same input dir).
- **Versions** — no wiring change; the Python starts *reading* the
  `--versions` file it already receives.

### 2.3 `generate_qc_report.py` changes

- New CLI args: `--run-summary run_summary.json`, `--distance-plots
  distance_plots/`, `--seg-qc seg_qc/` (warp-seg JSONs). `--versions` already
  exists.
- New parsers: `parse_run_summary_json`, `parse_versions_yml` (minimal,
  stdlib-only two-level YAML parse — no PyYAML dependency, matching the report's
  existing self-contained ethos), `parse_seg_qc_json`.
- New section builders: `run_summary_section`, `status_strip_section`,
  `manifest_section`, `versions_section`, `seg_overlay_section`; extend
  `registration_qc_section` with the histogram grid + warp-seg table.
- `copy_data` extended to archive the new artifacts alongside the existing ones.
- All new sections degrade gracefully: missing input → styled "not available"
  notice, never an exception (matches current `list_files` behaviour).

---

## 3. Deliverable B — Computational-resources report

### 3.1 Artifact & trigger

- New `bin/generate_resource_report.py` → self-contained HTML
  (`mirage_resource_report.html`), same embedded/dependency-free style as the QC
  report.
- Triggered from a **`workflow.onComplete`** block in `main.nf`, which fires
  after the DAG completes and after Nextflow has flushed `trace.txt` — the only
  point at which the full trace is available.
- The hook is **best-effort**: it shells out to the script and, on any failure
  (script error, no python3 on the head node, missing inputs), logs a warning
  and lets the run finish green. The script is a normal CLI, so it is also
  **re-runnable by hand** against an existing `outdir` + `.trace/`.
- Gated by `params.enable_trace` (the size logs and trace only exist then).

### 3.2 Inputs

- `input_sizes.csv` — header `process,sample_id,filename,bytes` (from
  `AGGREGATE_SIZE_LOGS`, published to `${outdir}/size_logs/`).
- `.trace/trace.txt` — TSV with `task_id, process, tag, name, status, exit,
  submit, start, complete, duration, realtime, %cpu, cpus, memory, peak_rss,
  peak_vmem, rchar, wchar`.

### 3.3 Content

1. **Run totals** — total wall-clock, total CPU-time, total tasks,
   succeeded/failed/retried counts, peak single-task RSS.
2. **Per-process rollup table** — task count, total & mean realtime, total
   CPU-time, mean/max `%cpu`, max `peak_rss`, max `peak_vmem`, total
   `rchar`/`wchar`, retry/fail counts.
3. **Resource vs input size** — join size logs to trace by `process` + `tag`
   (tag carries the sample id in this pipeline). Present `peak_rss` and
   `realtime` against input GB per task, so over/under-provisioned steps are
   visible. Rendered as sortable tables (+ optional inline SVG scatter, stdlib
   only — no plotting dependency).
4. **Top-N heaviest / slowest tasks** — by `peak_rss` and by `realtime`.
5. **Retries & failures** — every task with `attempt > 1` or non-zero `exit`,
   with exit status.
6. **Pointers** — links to Nextflow's native `report.html` / `timeline.html`
   for the interactive per-task views (no attempt to duplicate them).

### 3.4 Robustness

- Human-readable byte/duration formatting.
- Trace parsing tolerant of Nextflow's human units (`3.2 GB`, `12m 4s`, `1.2%`)
  → normalized to numbers.
- Missing or partial `trace.txt` / `input_sizes.csv` → render whatever is
  present with a notice; never crash, never fail the run.

---

## 4. Non-goals (YAGNI)

- No new heavyweight dependencies — both reports stay stdlib-only, matching the
  existing report.
- No change to *what QC each stage computes* — only to what the final report
  *surfaces*.
- Full-resolution registration QC TIFs (`*_QC_RGB_fullres.tif`) stay as data
  artifacts (too large to embed); referenced by filename only.
- Do not re-create the interactive views Nextflow's `report.html` /
  `timeline.html` already provide — link to them instead.

---

## 5. Files touched

**Deliverable A**
- `bin/generate_qc_report.py` — new sections, parsers, args (exec bit already 100755).
- `modules/local/generate_qc_report.nf` — new inputs + CLI flags.
- `workflows/mirage.nf` — build `run_summary.json`; forward distance plots,
  warp-seg QC; pass new inputs.
- `subworkflows/local/registration.nf` — add `distance_plots` emit.

**Deliverable B**
- `bin/generate_resource_report.py` — new script (must be tracked `100755`,
  because `main.nf` invokes it by name — see CLAUDE.md "When Adding a New Process").
- `main.nf` — `workflow.onComplete` best-effort invocation.

**Docs**
- `docs/parameters.md` — note the new report outputs; no new params expected
  (both reuse `skip_final_qc_report` / `enable_trace`).

---

## 6. Testing

- Python unit tests for the new parsers (versions.yml, run_summary.json,
  seg_qc.json, trace.txt unit normalization, size-log join) under `tests/`.
- Golden-ish smoke test: run each generator against a tiny fixture dir and
  assert the HTML contains the expected section headers and no unrendered
  placeholders.
- nf-test stub: `GENERATE_QC_REPORT` stub updated for the new inputs so the
  stub suite stays green; resource report exercised via a direct script call on
  a fixture (onComplete is not stub-exercised by nf-test).
