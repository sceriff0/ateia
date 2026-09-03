> **SUPERSEDED — historical record, 2026-09-02 (MIRAGE v1.0.0, release plan 13).**
> The ANHIR/ACROBAT landmark harness this document plans (`benchmarks/registration_eval/`)
> and the synthetic ground-truth rung that replaced it (`benchmarks/stare_bench/`) were
> both deleted and exist on no branch. `benchmarks/README.md` section B is the removal
> record. Nothing below is runnable; it is kept because it is the dated record of a
> decision that was actually taken. Do not edit the body — a June plan rewritten to
> describe a September tree is a lie about its own date.

# Mirage Benchmarking & Registration-Accuracy Framework — Design

**Date:** 2026-06-16
**Branch:** `benchmarking`
**Status:** Draft for review

## 1. Purpose & Scope

Build a self-contained benchmarking layer for the Mirage WSI pipeline that:

1. Generates a controlled **input matrix** (sizes × channels) from a single source image.
2. Sweeps the pipeline over that matrix **and over the pipeline parameters that influence cost**, capturing **resource usage** (RAM, runtime, CPU, I/O) vs input size *and* parameter settings, per process.
3. Evaluates **registration accuracy** against landmark ground truth (ANHIR, ACROBAT), comparing **classic vs tiled** VALIS modes.
4. Produces a **Jupyter notebook** with paper-ready plots and a resource-vs-size **linear regression**.
5. Derives an **optimal `modules.config`** (separate, reviewable file) from the regression.
6. Surfaces the results in the **Read the Docs** site.

### Deliverable boundary

This branch delivers the **harness, evaluator, notebook, config generator, and docs scaffolding**. The heavy compute (full WSI sweeps, real registration challenge data) runs on the **user's SLURM/GPU cluster**. A tiny synthetic fixture lets the notebook and tests run end-to-end in CI without real data.

### Non-goals

- No change to default production-pipeline behavior.
- No automatic overwrite of the live `conf/modules.config`.
- `bin/estimate_reg_gb.py` is **not** moved — it is load-bearing runtime routing, not telemetry.

## 2. Isolation & Branch Strategy

All new work lives under a new top-level `benchmarks/` tree. Edits to existing files are strictly additive or gated:

- `bin/register.py` — publish VALIS `*_summary.csv` + `*_registrar.pickle` as outputs (already produced internally; just emit them). Opt-in, no behavior change when unused.
- `modules/local/register.nf` + `conf/modules.config` — add `publishDir` patterns for the two registrar artifacts.
- Size-log telemetry — see §6 (relocated + gated).
- `mkdocs.yml` — one nav entry; `docs/benchmarks.md` — new page.

## 3. Directory Layout

```
benchmarks/
  README.md                          # how to run a sweep + reproduce figures
  generate_matrix.py                 # source image -> (size x channel) OME-TIFF matrix + samplesheets
  run_sweep.sh                       # driver: launch pipeline once per matrix cell with enable_trace
  configs/
    benchmark.config                 # `-profile benchmark`: enable_trace, isolated trace_dir per run
  modules/
    aggregate_size_logs.nf           # MOVED here from modules/local/ (see §6)
  registration_eval/
    landmarks.py                     # generic (x,y) landmark loader (one model, two adapters)
    adapters/anhir.py                # rTRE = dist / image-diagonal; MMrTRE/AMrTRE
    adapters/acrobat.py              # error in µm (needs pixel size); mean 90th percentile
    prepare_pairs.py                 # challenge data -> Mirage samplesheet
    eval_tre.py                      # load registrar pickle -> warp_xy landmarks -> TRE/rTRE/µm
  analysis/
    benchmark_analysis.ipynb         # the notebook (5 sections)
    lib/
      parse.py                       # trace.txt + input_sizes.csv -> tidy dataframe
      regress.py                     # per-process resource ~ size regression (linear + log-log)
      sampling.py                    # bootstrap CIs, paired tiled-vs-classic tests
      plotting.py                    # shared paper-ready matplotlib theme + PDF/SVG export
    emit_config.py                   # regression coeffs -> conf/modules.optimized.config
    figures/                         # exported PDF/SVG (consumed by docs)
  fixtures/
    synthetic/                       # tiny precomputed trace/size/landmark data for CI + notebook smoke
  tests/
    test_generate_matrix.py
    test_landmarks.py                # rTRE & µm math
    test_parse.py
    test_regress.py
```

## 4. Component A — Rescaling Matrix Generator

`generate_matrix.py`:

- **Input:** one source image (any format pyvips reads).
- **Size axis:** resize to target long-edge in pixels, default `{2048, 4096, 8192, 16384, 32768}` (configurable). Produces a clean, monotonic input-byte / pixel-count axis for regression.
- **Channel axis:** default `{1, 2, 4, 8}`. Channels synthesized by duplicating the source channel with **per-channel intensity jitter + Gaussian noise + sub-pixel offset**, so each channel is non-identical (stress-tests registration/segmentation/quantification without real multiplex data).
- **Output:** one OME-TIFF per `(size, channels)` cell, with correct OME-XML channel names so the existing OME-channel matching in `subworkflows/local/adapters/valis_adapter.nf` works unchanged.
- **Manifest:** `matrix_manifest.csv` with `cell_id, target_px, width, height, n_channels, bytes, path` so the notebook joins runtime → true input dimensions (not just file bytes).
- **Tooling:** pyvips (resize) + tifffile/ome-types (OME-TIFF + channel metadata).

### 4.1 Parameter sweep axis

Beyond image size/channels, the sweep covers the **pipeline parameters that drive runtime / peak RSS**, declared in a config-driven `benchmarks/configs/sweep.yaml`:

```yaml
strategy: ofat        # ofat (one-factor-at-a-time, default) | grid (full cross product)
image:
  target_px:  [2048, 4096, 8192, 16384, 32768]
  n_channels: [1, 2, 4, 8]
params:                # name -> values; each maps to a --<param> override
  # registration
  memory_mode:                 [low, medium, high]
  reg_max_image_dim:           [2000, 4000, 8000]
  reg_use_tiled_registration:  [true, false]
  reg_tile_size:               [1024, 2048, 4096]
  reg_n_workers:               [2, 4, 8]
  reg_parallel_warping:        [true, false]
  skip_micro_registration:     [true, false]
  reg_distributed_tiling:      [false, true]
  reg_dist_tile_wh:            [256, 512, 1024]
  reg_dist_force_tiling:       [false, true]
  # preprocessing / segmentation / quantification / pyramid
  preprocess_tile_size:        [...]
  seg_method:                  [...]
  expanded_quantification:     [true, false]
  pyramid_tile_size:           [...]
```

- **`ofat` (default):** vary one parameter at a time off a baseline cell — keeps run count linear and gives clean per-parameter sensitivity (and clean single-predictor regression slices). Logged so the notebook knows which axis each run varied.
- **`grid`:** full cross product for selected axes when interactions matter — the user opts in per axis (run-count guardrail: the driver prints the planned run count and requires confirmation above a threshold).
- The exact value lists are tunable; the file ships with sensible defaults and inline comments. Axes the user doesn't care about are commented out.

`run_sweep.sh` enumerates the sweep into a **run plan** (`run_plan.csv`: `run_id, cell_id, varied_axis, param overrides…`), then launches the existing pipeline once per run with `-profile benchmark`, `--enable_trace true`, `--enable_size_logs true`, the per-run `--<param>` overrides, and a per-run `trace_dir` + `outdir`, so each run isolates its `trace.txt` + `size_logs/input_sizes.csv`. The `run_id`/`varied_axis` columns let the notebook attribute cost to the right factor.

## 5. Component B — Registration-Accuracy Evaluation

### 5.1 Ground-truth ingest

- `landmarks.py`: one in-memory landmark-pair model `(moving_xy[N,2], target_xy[N,2])`.
- `adapters/anhir.py`: parse ANHIR landmark CSVs; reduce with **rTRE = Euclidean(warped, target) / image_diagonal**; report median, MMrTRE, AMrTRE.
- `adapters/acrobat.py`: parse ACROBAT landmarks (multi-annotator); reduce in **µm** using pixel size; report mean **90th percentile**.
- `prepare_pairs.py`: emit a Mirage samplesheet for each challenge image pair.

#### Data access (verified 2026-06-17)

- **ANHIR** — fully **account-gated**: requires a grand-challenge.org account → join the challenge → accept CC-BY-NC-SA → download from the platform. No direct/automatable URL; landmark CSVs are bundled with the (multi-scale) images, not separately downloadable. **User must download.**
- **ACROBAT** — WSIs are **open access** on the Swedish National Data Service (direct ZIPs at `https://api.researchdata.se/dataset/2022-190-1/...`), but the **landmark annotations are NOT in the SND record** — they live behind the challenge site (account-gated), and the WSI archives are very large (4,212 WSIs). **User downloads the landmarks (and chosen WSIs).**
- Consequence: the evaluator is **format-driven and download-independent**. The user points `prepare_pairs.py` at a local data dir; `benchmarks/registration_eval/README.md` documents the exact access steps for reproducibility. A tiny synthetic landmark fixture covers CI.

### 5.2 True TRE via VALIS warp

VALIS already saves `{name}_registrar.pickle` and `Slide.warp_xy(xy, …)` warps coordinates through registered space. `eval_tre.py`:

1. Load the registrar pickle for a registered pair.
2. `warp_xy` the **moving** ground-truth landmarks into the target frame.
3. Compute **TRE (px), rTRE (px / diagonal), µm (× pixel size)** per landmark.

Run for **both** modes: classic (`reg_distributed_tiling=false`) and distributed/tiled.

### 5.3 Three-way error comparison

Per pair, per mode, collect:

- **(a) True TRE** — from warped ground-truth landmarks (§5.2).
- **(b) VALIS self-estimate** — `*_rTRE` columns (`original/rigid/non_rigid_rTRE`) from VALIS `summary_df` / `{name}_summary.csv` (already computed by `Valis.measure_error()`).
- **(c) Feature-distance estimate** — existing `bin/estimate_feature_distances.py` output.

### 5.4 Required `register.py` change (opt-in, additive)

Publish the already-produced `{name}_summary.csv` and `{name}_registrar.pickle` as process outputs (new `publishDir` patterns in `modules/local/register.nf` / `conf/modules.config`). No new VALIS calls; no default-behavior change.

### 5.5 Sampling

`sampling.py`: bootstrap landmark subsets → CIs for each of (a)/(b)/(c); paired test (classic vs tiled) on matched pairs. Quantifies whether the tiled path's accuracy difference is significant, and how well VALIS's self-estimate and the feature estimate track true TRE.

## 6. Component — Size-Log Telemetry Relocation

The `*.size.csv` subsystem exists **only** for profiling/benchmarking (it does not drive routing — that is `estimate_reg_gb.py`). It therefore belongs to the benchmark layer.

- **Move** `modules/local/aggregate_size_logs.nf` → `benchmarks/modules/aggregate_size_logs.nf`; update the include in `workflows/mirage.nf`.
- **Gate** per-module size emission behind a new dedicated `params.enable_size_logs` (default **off** in production; set **on** by `-profile benchmark`). The inline emitters stay in their process bodies (they cannot physically relocate) but become benchmark-gated and conditionally wired in the subworkflows.
- All **consumption** (parsing/joining/analysis) lives in `benchmarks/analysis/lib/parse.py`.

## 7. Component C — Analysis Notebook

`benchmark_analysis.ipynb`, thin cells calling `analysis/lib/`:

1. **Ingest** — glob all per-run `trace.txt` + `input_sizes.csv` + `matrix_manifest.csv` → tidy dataframe.
2. **Resource ~ size + parameter regression** — per process, fit `peak_rss` & `realtime` against input bytes / pixels / channels **and the swept parameters** (`run_plan.csv` columns as predictors: numeric params as continuous, booleans/enums as categoricals). Linear + log-log, with prediction intervals; per-parameter sensitivity from the `ofat` slices. Feeds §8.
3. **Tiled vs classic** — true TRE + VALIS estimate + feature estimate, plus cost (`peak_rss`, `realtime`) across input sizes.
4. **Sampling** — bootstrap distributions / CIs; paired tiled-vs-classic test.
5. **Paper-ready styling** — shared theme; export every figure to `figures/*.pdf` + `*.svg`.

Runs end-to-end on `fixtures/synthetic/` so CI can execute it headless (`jupyter nbconvert --execute`).

## 8. Component D — Optimal `modules.config`

`emit_config.py` reads exported regression coefficients and generates **`conf/modules.optimized.config`** — a separate, reviewable file with derived dynamic `memory = { … }` / `time = { … }` closures (fit + safety margin, respecting existing `check_max`/`task.attempt` scaling). It never overwrites the live config; the user diffs and adopts deliberately. A short report lists, per process, the fitted formula and the margin applied.

## 9. Component E — Docs

- New `docs/benchmarks.md` under "User Guide" in `mkdocs.yml` nav.
- Embeds exported figures via the existing glightbox + `docs/assets/images/` convention (fills some currently-commented image slots).
- Subsections: input matrix, resource scaling + regression, registration accuracy (classic vs tiled, three-way error), how the optimal config was derived (links regression → config).

## 10. Testing

- pytest: matrix generator (shapes/channel metadata), landmark loader + rTRE/µm math (highest risk), trace/size parser, regression fitter (on synthetic data with a known slope).
- Notebook smoke test in CI via `nbconvert --execute` on the synthetic fixture.
- No nf-test changes required for default runs; an optional stub test that `-profile benchmark` still launches.

## 11. Data Flow Summary

```
source image ─generate_matrix.py─▶ matrix OME-TIFFs + samplesheets + manifest
                                         │
                                  run_sweep.sh (per cell, -profile benchmark)
                                         │
              ┌──────────────────────────┴───────────────────────────┐
        trace.txt (per run)                              size_logs/input_sizes.csv (gated)
              └──────────────┬───────────────────────────┬───────────┘
                             ▼                            │
                    analysis/lib/parse.py ◀───────────────┘ + matrix_manifest.csv
                             │
        ┌────────────────────┼─────────────────────────────┐
   regress.py           sampling.py                    plotting.py
        │                                                    │
   emit_config.py                                      figures/*.pdf,*.svg
        ▼                                                    ▼
 conf/modules.optimized.config                        docs/benchmarks.md

registration challenge data ─prepare_pairs.py─▶ samplesheet ─pipeline (classic & tiled)─▶
   {name}_registrar.pickle + {name}_summary.csv ─eval_tre.py─▶ true TRE / VALIS est / feature est
                                                          └─▶ sampling.py ─▶ figures
```

## 12. Resolved Decisions

1. **Size-log gating:** new dedicated `params.enable_size_logs` (default off in production; on under `-profile benchmark`). (§6)
2. **Source image:** the **user supplies** it; `generate_matrix.py` takes it as a required argument (no bundled default).
3. **Challenge data:** I attempt to download ANHIR/ACROBAT; if gated/impractical, the user provides it and points `prepare_pairs.py` at the local path. Either way the loader is format-driven, not download-dependent.
4. **Parameter sweep:** the benchmark covers pipeline parameters that influence runtime/peak RSS (registration memory_mode, tiling, dims, workers; preprocessing/segmentation/quantification/pyramid knobs), `ofat` by default with opt-in `grid`. (§4.1)
