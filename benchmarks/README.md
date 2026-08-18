# Mirage Benchmarking

Produces the **DATA** a method paper needs — tidy CSV tables, not plots — for the
*shipped* pipeline (classic VALIS registration). Three signals, all harvested from
QC the pipeline already emits (the benchmark invents no metrics):

1. **Resource scaling** — peak RAM & wall-time vs input size / channels / rounds (Nextflow trace).
2. **Registration accuracy** — `dice_matched` + centroid **displacement (µm)** from the staged
   DAPI-nuclei QC (`reg_qc=2`; `bin/warp_seg_qc.py`). Landmark-free.

The heavy step (the sweep) runs on a **cluster** with the pipeline containers;
matrix generation and the DATA emit run **locally**.

```
   generate_matrix.py --sweep  ->  build_run_plan.py --sweep  ->  run_sweep.sh  (cluster)
                                                                       |
                                             per-run trace + size logs + QC JSONs
                                                                       |
                                        make_tables.py  ->  benchmarks/paper_data/*.csv (+ *.dict.md)
```

!!! note
    The distributed/tiled registration path was archived out of the pipeline (git tag
    `archive/tiled-valis-2026-07-24`); registration is classic `REGISTER` only, so the
    old classic-vs-distributed benchmark was removed.

!!! note "This is the SYNTHETIC sweep"
    It measures how cost SCALES. To choose a configuration — an arm ranking on the
    REAL study slides — see `docs/benchmarks_real.md` (`benchmarks/configs/arms.yaml`).

**Where the plots come from.** This layer emits **data**, plus `scaling_*.pdf/svg`
from `make_figures`. Every other figure is rendered by the consumer,
`../ihc_method`, whose `code/benchmark_plots.R` + `code/registration_accuracy_plots.R`
are the maintained descendants of a `plots.R` that used to live here. That copy was
deleted rather than kept in sync: it was referenced by nothing, tested by nothing,
and had drifted well behind the two files that actually render.

Design record: `docs/superpowers/specs/2026-07-24-benchmark-paper-data-design.md`.
Rendered overview: `docs/benchmarks.md`.

## Prerequisites

- **Local (DATA emit + tests):** numpy, pandas, scikit-learn, tifffile, PyYAML.
  (`matplotlib` only for the optional figures.) No real data
  needed for the test suite.
- **Cluster (the sweeps):** Nextflow >= 25.04, Docker/Singularity, the pipeline
  containers (including the VALIS image), and `bash`. `run_sweep.sh` /
  `run_registration.sh` use indexed-array lookups (no `declare -A`), so macOS
  `/bin/bash` 3.2 works for the tested paths; if you hit an array error, `brew install bash`.

Verify the whole harness with no data at all:

    python -m pytest benchmarks/tests -q          # -> all green on synthetic fixtures

---

## A. Resource / parameter sweep

### A1 — Generate the input matrix

    python benchmarks/generate_matrix.py \
        --source /path/to/your_source.ome.tif \
        --outdir bench_matrix \
        --sweep benchmarks/configs/sweep.yaml

- **`--sweep` is the easy path:** it reads the sweep and generates exactly the cells +
  moving panels the sweep needs — deriving `--target-px`, `--n-channels`, `--paired`, and
  `--n-moving` (= `max(n_register_images) - 1`) automatically. Input-scale cells come from
  the sweep's `scaling_grid` (sizes × channels); the shipped sweep caps size at **65536**
  and channels at **{2, 4}** (1-channel isn't benchmarked), so `--sweep` builds a **6 × 2 =
  12-cell** matrix. Moving panels are generated **per cell, plan-aware**: each cell gets
  exactly as many panels as the runs that touch it consume (derived from the real run
  configs). Cells that only pair-register get 1; cells the `registration_grid` runs at 8
  rounds get 7 — including the big ones, since those *are* measured at multiple rounds. No
  panels are made that no run opens. No manual sync between the matrix and the sweep.
  (Explicit `--target-px` / `--n-channels` / `--n-moving` (forces a uniform count) /
  `--paired` still override; `--seed 0` by default.)
- **Input:** one image you supply (any Bio-Formats/tifffile-readable format; **uint8 or uint16**).
- **Output:** `bench_matrix/px{px}_ch{n}.ome.tif` per (size x channel) cell; with pairing,
  also `px{px}_ch{n}_moving{j}.ome.tif` (j = 1..n-moving) for n>=2 cells, each a distinct
  panel with distinct channel names; plus `bench_matrix/matrix_manifest.csv`
  (`cell_id,target_px,width,height,n_channels,bytes,path[,moving_paths]`, where `moving_paths`
  is a `;`-joined list). The N-image registration sweep (`n_register_images: [2, 4, 8]`)
  needs ≥7 moving panels — `--sweep` provides them.

### A2 — Expand the sweep into a run plan

    python benchmarks/build_run_plan.py \
        --sweep benchmarks/configs/sweep.yaml \
        --out bench_run_plan.csv \
        --repeats 3                       # replicate runs per config (default 3)

- **Input:** `benchmarks/configs/sweep.yaml` — edit it: `strategy: ofat|grid`, a `baseline:`
  map, the OFAT `axes:` (one knob varied off baseline), and two input-scaling grids:
  `scaling_grid:` (`target_px` × `n_channels`) and `registration_grid:` (`target_px` ×
  `n_register_images`, at a fixed channel count). Both are full cross-products because the
  three input-scaling dimensions — pixels, channels-per-round, and **number of registered
  rounds** — each drive memory (REGISTER's input is the sum of all rounds; merged channels
  grow with round count), so each must be measured across sizes, not at one baseline cell.
  Do NOT also list `target_px` / `n_channels` / `n_register_images` under `axes:` — the
  grids own input scale, and double-listing would confound the regression.
- **A knob that is only live under another setting must NOT be a flat OFAT axis.** Varying
  it off the baseline changes a value the pipeline never reads: the run costs full price and
  measures nothing. `sweep.yaml` has three structures for these, and
  `test_project_sweep_has_no_dead_axes` enforces the rule:
  - `segmentation_grid:` — pins `seg_method` per backend and crosses that backend's own knobs
    (StarDist tile grid; InstanSeg tile size × batch size; CellSAM block size × bbox threshold).
    All three shipped backends are covered.
  - `registration_method_grid:` — pins `registration_method` and crosses that method's knobs.
    The `tiled`/STARE entry crosses `reg_tiled_tile` × `reg_tiled_gate_tre` ×
    `reg_tiled_coarse_max_dim`. The last of these is the resolution STARE solves its global `M0`
    at — the counterpart to VALIS's `reg_max_image_dim`; sweeping one and not the other would
    compare a *tuned* VALIS against an *untuned* STARE, so
    `test_project_stare_resolution_axis_mirrors_the_valis_one` requires it to be swept and to
    bracket its default in both directions. STARE has a single execution shape (the per-tile
    fan-out `TILED_COARSE`/`TILED_REG_TILE`/`TILED_SOLVE`/`TILED_STITCH`); the
    `reg_tiled_fanout` flag and the single-task `TILED_REGISTER` path it selected were removed
    upstream, so there is no execution shape left to cross.
- **The `baseline:` map must equal the shipped `nextflow.config` defaults**, and must declare
  every param any grid or axis varies (so all configs share CSV columns).
  `test_project_sweep_baseline_matches_pipeline_defaults` reads `nextflow.config` directly and
  fails on any divergence — it is not a hand-maintained mirror.
- Some axes are **measurement settings**, not pipeline knobs: `reg_qc` changes *what is
  measured*, so its runs are comparable on cost but **not** on quality. Keep the cohort at the
  baseline value for any quality table. It carries this caveat inline.
- **`--repeats N`** emits N replicate launches per config so the analysis can report
  per-config variance (timing at n=1 is noisy — cache state, node contention). `N=3` is the
  default; `N=1` is a single-shot plan. Replicates share a `config_id`; `rep` is the index.
  Repeats multiply *runs*, not *images* (the cell image is reused across a config's reps).
- **Output:** `bench_run_plan.csv` — one row per pipeline run
  (`run_id,varied_axis,config_id,rep,<param columns incl target_px,n_channels>`).

### A3 — Launch the sweep (cluster)

    benchmarks/run_sweep.sh  bench_run_plan.csv  bench_matrix/matrix_manifest.csv  bench_results \
        [extra nextflow args, e.g. -profile singularity]

- **What it does:** for each run-plan row, resolves the cell image, writes a per-run
  samplesheet (reference + `n_register_images-1` moving panels when paired), and launches the pipeline once with
  `-profile docker -c benchmarks/configs/benchmark.config` (enables `enable_trace` +
  `enable_size_logs`), isolating each run's outputs.
- **Output per run:**
  - `bench_results/<run_id>/trace/trace.txt` — peak_rss, realtime, cpus (+ `report.html`, `timeline.html`)
  - `bench_results/<run_id>/out/size_logs/input_sizes.csv` — per-process input bytes
  - `bench_results/<run_id>/out/...` — the normal pipeline outputs

> `run_sweep.sh` parses CSV headers with indexed bash arrays. macOS `/bin/bash` (3.2)
> works; if you see an array error use a newer bash (`brew install bash`).

> **Registration in the resource sweep.** With `--paired`, `run_sweep.sh` emits a
> reference + one-or-more moving panels per patient with **distinct channel names**
> (reference `DAPI|ch1|...`, moving panel _p_ `DAPI|m{p}_1|...` — distinct so the
> pipeline's duplicate-channel guard accepts them), so VALIS registration runs and the
> registration axes (`memory_mode`, `reg_micro_reg`, `n_register_images`) are
> exercised. **`n_register_images`** (default `[2, 4, 8]`) sets how many slides register
> together (1 reference + N−1 moving panels) — this is how registration is benchmarked on
> N images, not just a pair. Generating the matrix with `--sweep` provides the moving
> panels automatically (no `--n-moving` to compute).
> (`reg_use_tiled_registration`, `reg_tile_size`, `reg_n_workers`, `reg_parallel_warping`
> were dropped — VALIS auto-tiles internally and warping is sequential, so they had no
> effect.) Pairing applies to n_channels >= 2 cells; n_channels == 1 cells are
> reference-only. Without `--paired`, registration is a single-slide passthrough.

### A4 — Emit the paper DATA (local)

    python -m benchmarks.analysis.make_tables \
        --results-root bench_results \
        --run-plan     bench_run_plan.csv \
        --outdir       benchmarks/paper_data

- **Output:** `benchmarks/paper_data/{runs_master,scaling_fits,registration_accuracy,
  registration_valis_rtre,segmentation_agreement,param_matrix}.csv`,
  each with a sibling `.dict.md` data dictionary. **This is the method-paper deliverable.**
  Column-by-column meaning is in each `.dict.md` and summarised in `docs/benchmarks.md`.
- **Two registration-accuracy signals:** `registration_accuracy.csv` (segmentation-based
  Dice/displacement from `reg_qc: 2`) and `registration_valis_rtre.csv` (VALIS's *own*
  feature-based rTRE, harvested from the summary CSVs it writes — the same numbers the
  final QC report shows). Both need registration to have actually run (paired matrix).

### A5 — Optional: figures + derived config (local)

    python -m benchmarks.analysis.make_figures \
        --results-root bench_results \
        --run-plan     bench_run_plan.csv \
        --outdir       benchmarks/analysis

> **Run it any time — the sweep does not have to be finished.** Both `make_tables` and
> `make_figures` read whatever
> runs have completed: it skips run dirs with no `trace.txt` yet, and (because Nextflow only writes
> a trace row when a *process* finishes) an in-flight run contributes just its completed processes.
> `only_successful` then drops any non-`COMPLETED` row, so the fits/means never see a partial process.
> Early on, size-varying runs may be too few for a real fit (`r2` empty ⇒ `n<3` flat fallback) — that
> is expected, not an error. Re-run it as more runs land to watch the fits firm up.

- **Output:**
  - `benchmarks/analysis/measurements.csv` — **the primary data artifact**: one tidy row per
    (run × process) with `peak_rss_gb`, `peak_vmem_gb`, `realtime_s`, `duration_s`, `cpus`,
    `input_gb`, and every swept param column joined in (`run_id`, `varied_axis`, `target_px`,
    `n_channels`, …). Read straight into R (`read.csv`); `../ihc_method` consumes exactly this file.
  - `benchmarks/analysis/resource_models.csv` — one row per process with the fitted
    `slope`, `intercept`, `r2`, `sigma`, `n` (empty `r2` ⇒ `n<3`, flat fallback). Fit uses
    only the size-varying runs (`scaling_grid` + `baseline`); OFAT-knob runs are excluded so
    they don't pile points at one input size and confound the regression.
  - `benchmarks/analysis/resource_stats.csv` — per-`(process, config_id)` replicate
    variance: `n_reps` and `mean`/`std`/`cv` of `peak_rss_gb`, `realtime_s`, etc. This is the
    error bar `--repeats` buys you (empty/degenerate when the plan has no replicates).
  - `benchmarks/analysis/figures/scaling_<PROCESS>.pdf` + `.svg` — peak-RSS-vs-input fits per process.
  - `benchmarks/analysis/modules.optimized.config` — regression-derived memory directives,
    `memory = { ( <input_gb>*slope + intercept + sigma*task.attempt ).GB }`. No `check_max()`
    wrapper — that helper does not exist in this repo; `process.resourceLimits` (top-level in
    `nextflow.config`) clamps every request against `params.max_*` centrally.
    Processes with a known input expression are **live** blocks; others are emitted as
    **commented** blocks for you to fill in (so the file is valid Groovy as-is). It never
    overwrites the live config — review and diff:

        diff conf/modules.config benchmarks/analysis/modules.optimized.config

---

## B. Optional: external landmark validation (ANHIR / ACROBAT)

> **Not the paper's accuracy signal.** Registration accuracy for the paper comes from the
> pipeline's own `reg_qc=2` staged QC, harvested into `paper_data/registration_accuracy.csv`
> (Section A) — no challenge data required. This section is a separate, optional cross-check
> against public landmark ground truth, and the only place a *ground-truth* number exists at
> all. It now registers each pair with BOTH shipped methods (`valis`, `tiled`/STARE), so the
> landmark TRE can be read against each method's own self-reported accuracy.

Full detail (data access, limitations) in `benchmarks/registration_eval/README.md`.

### B0 — Get data + describe pairs

ANHIR is account-gated; ACROBAT landmarks are challenge-gated — download with your own
account. Then write a `pairs.csv`:

    pair_id,ref_image,moving_image,source_landmarks,target_landmarks,width,height,pixel_size_um
    P001,/data/P001_HE.tif,/data/P001_IHC.tif,/data/P001_mov.csv,/data/P001_ref.csv,40000,30000,0.25

### B1 — Prepare per-pair input dirs

    python -m benchmarks.registration_eval.prepare_pairs --pairs-csv pairs.csv --out reg_prepared

- **Output:** `reg_prepared/<pair_id>/input/` (symlinked images) + `reg_prepared/pairs_manifest.csv`.

### B2 — Register (valis & STARE) + score (cluster, VALIS env)

    # 4th arg is optional: a StarDist model dir enables the reg_qc=2 seg-overlap leg
    benchmarks/registration_eval/run_registration.sh \
        reg_prepared/pairs_manifest.csv  reg_prepared  reg_results  [stardist_model_dir]

- **What it does:** per pair x method, registers with `bin/register.py` (`valis`) and
  `bin/tiled_register.py` (`tiled`/STARE), then `eval_tre` warps the moving ground-truth
  landmarks through that method's transform. With a model dir it also segments both native
  slides and runs `bin/warp_seg_qc.py` — the pipeline's own reg_qc=2 scorer — per method.
- **Output:** `reg_results/eval_<pair>_<method>.json` — ground truth plus every
  method-native signal available for that run:
  | field | source | available for |
  |---|---|---|
  | `true_tre` | landmark TRE/rTRE/um | both (the only ground truth) |
  | `valis_rtre` | VALIS `error_df` | `valis` |
  | `stare_tre` | STARE intrinsic `_tre.json` (coarse / rigid / post-mesh residual) | `tiled` |
  | `seg_qc` | reg_qc=2 dice + centroid displacement (`bin/warp_seg_qc.py`) | both, if a model dir was given |

### B3 — Aggregate

    python -m benchmarks.registration_eval.aggregate_eval \
        --eval-dir reg_results --out reg_eval.csv --agg-out reg_eval_agg.csv

- **Output:** `reg_eval.csv` (per pair/mode tidy rows) + `reg_eval_agg.csv` (per-mode
  **MMrTRE / AMrTRE**). `reg_eval.csv` is what `make_figures --reg-eval` consumes.

---

## Inputs -> outputs at a glance

| Step | You provide | You get |
|---|---|---|
| `generate_matrix.py` | 1 source image | matrix of OME-TIFFs + `matrix_manifest.csv` |
| `build_run_plan.py` | `sweep.yaml` | `run_plan.csv` |
| `run_sweep.sh` | manifest + run plan | per-run `trace.txt` + `input_sizes.csv` + QC JSONs (`*_seg_qc.json`) |
| **`make_tables`** | results + run plan | **`paper_data/{runs_master,scaling_fits,registration_accuracy,registration_valis_rtre,segmentation_agreement,param_matrix}.csv` (+ `.dict.md`)** — the paper DATA |
| `make_figures` (optional) | results + run plan | `measurements.csv` + `resource_models.csv` + `resource_stats.csv` + `scaling_*.pdf/svg` + `modules.optimized.config` |
| `prepare_pairs.py` (optional) | `pairs.csv` (+ downloaded data) | per-pair input dirs + `pairs_manifest.csv` |
| `run_registration.sh` (optional) | pairs manifest | `eval_*.json` (landmark TRE) |
| `aggregate_eval` (optional) | eval JSONs | `reg_eval.csv` + `reg_eval_agg.csv` |

The fastest way to see every component work end-to-end **without any real data** is
`python -m pytest benchmarks/tests -q` — the suite drives the whole harness on tiny
synthetic fixtures.
