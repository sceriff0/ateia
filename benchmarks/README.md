# Mirage Benchmarking

Quantifies **resource usage vs input size + pipeline parameters**, and **registration
accuracy** (in-process tiling on vs off) against landmark ground truth — then derives
an optimised `modules.config` from the measured scaling.

There are **two independent pipelines** (A: resource sweep, B: registration accuracy)
plus an analysis layer (C: notebook, D: docs figures). The heavy steps (the sweeps)
run on a **cluster** with the pipeline containers; everything else runs **locally**.

```
A. RESOURCE / PARAMETER SWEEP          B. REGISTRATION ACCURACY
   generate_matrix → build_run_plan       prepare_pairs
        |                |                      |
        +-- run_sweep.sh +                 run_registration.sh
                |                               |
          make_figures  <----- reg_eval.csv --- aggregate_eval
                |
   figures + modules.optimized.config           (both feed the notebook, C)
```

Design rationale: `docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md`.
Rendered overview: `docs/benchmarks.md`.

## Prerequisites

- **Local (analysis + tests):** numpy, pandas, scikit-learn, matplotlib, tifffile,
  pyvips *or* Pillow, PyYAML, nbformat. No real data needed for the test suite.
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
        --paired --n-moving 7
        # optional: --target-px 2048 4096 8192 16384 32768 65536 131072  --n-channels 1 2 4 8  --seed 0

- **Input:** one image you supply (any Bio-Formats/tifffile-readable format; **uint8 or uint16**).
- **Output:** `bench_matrix/px{px}_ch{n}.ome.tif` per (size x channel) cell; with `--paired`,
  also `px{px}_ch{n}_moving{j}.ome.tif` (j = 1..`--n-moving`) for n>=2 cells, each a distinct
  panel with distinct channel names; plus `bench_matrix/matrix_manifest.csv`
  (`cell_id,target_px,width,height,n_channels,bytes,path[,moving_paths]`, where `moving_paths`
  is a `;`-joined list).
- Use `--paired` so registration actually runs in the sweep (see the note below).
- Set `--n-moving K` to benchmark **N-image registration**: K must be `>= max(n_register_images)-1`
  in `sweep.yaml` (default sweep goes up to 8 panels, so `--n-moving 7`). More movings = more
  disk; drop it to 1 if you only need the classic ref+moving pair.

### A2 — Expand the sweep into a run plan

    python benchmarks/build_run_plan.py \
        --sweep benchmarks/configs/sweep.yaml \
        --out bench_run_plan.csv

- **Input:** `benchmarks/configs/sweep.yaml` — edit it: `strategy: ofat|grid`, a `baseline:`
  map, and the `axes:` you want. (Registration axes only do work when the matrix was `--paired`.)
- **Output:** `bench_run_plan.csv` — one row per pipeline run
  (`run_id,varied_axis,<param columns incl target_px,n_channels>`).

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
> registration axes (`memory_mode`, `skip_micro_registration`, `n_register_images`) are
> exercised. **`n_register_images`** sets how many slides register together (1 reference +
> N−1 moving panels) — this is how registration is benchmarked on N images, not just a
> pair; it needs the matrix built with `--n-moving >= max(n_register_images)-1`.
> (`reg_use_tiled_registration`, `reg_tile_size`, `reg_n_workers`, `reg_parallel_warping`
> were dropped — VALIS auto-tiles internally and warping is sequential, so they had no
> effect.) Pairing applies to n_channels >= 2 cells; n_channels == 1 cells are
> reference-only. Without `--paired`, registration is a single-slide passthrough.

### A4 — Analyse: figures + optimal config (local)

    python -m benchmarks.analysis.make_figures \
        --results-root bench_results \
        --run-plan     bench_run_plan.csv \
        --manifest     bench_matrix/matrix_manifest.csv \
        --outdir       benchmarks/analysis
        # optional: --reg-eval reg_eval.csv   (from pipeline B, for the accuracy section)

- **Output:**
  - `benchmarks/analysis/measurements.csv` — **the primary data artifact**: one tidy row per
    (run × process) with `peak_rss_gb`, `peak_vmem_gb`, `realtime_s`, `duration_s`, `cpus`,
    `input_gb`, and every swept param column joined in (`run_id`, `varied_axis`, `target_px`,
    `n_channels`, …). Read straight into R (`read.csv`) — no notebook required.
  - `benchmarks/analysis/resource_models.csv` — one row per process with the fitted
    `slope`, `intercept`, `r2`, `sigma`, `n` (empty `r2` ⇒ `n<3`, flat fallback).
  - `benchmarks/analysis/figures/scaling_<PROCESS>.pdf` + `.svg` — peak-RSS-vs-input fits per process.
  - `benchmarks/analysis/modules.optimized.config` — regression-derived memory directives,
    `memory = { check_max( ( <input_gb>*slope + intercept + sigma*task.attempt ).GB, 'memory' ) }`.
    Processes with a known input expression are **live** blocks; others are emitted as
    **commented** blocks for you to fill in (so the file is valid Groovy as-is). It never
    overwrites the live config — review and diff:

        diff conf/modules.config benchmarks/analysis/modules.optimized.config

---

## B. Registration accuracy (in-process tiling on vs off)

Full detail (data access, limitations) in `benchmarks/registration_eval/README.md`.

### B0 — Get data + describe pairs

ANHIR is account-gated; ACROBAT landmarks are challenge-gated — download with your own
account. Then write a `pairs.csv`:

    pair_id,ref_image,moving_image,source_landmarks,target_landmarks,width,height,pixel_size_um
    P001,/data/P001_HE.tif,/data/P001_IHC.tif,/data/P001_mov.csv,/data/P001_ref.csv,40000,30000,0.25

### B1 — Prepare per-pair input dirs

    python -m benchmarks.registration_eval.prepare_pairs --pairs-csv pairs.csv --out reg_prepared

- **Output:** `reg_prepared/<pair_id>/input/` (symlinked images) + `reg_prepared/pairs_manifest.csv`.

### B2 — Register (tiled & untiled) + score (cluster, VALIS env)

    benchmarks/registration_eval/run_registration.sh \
        reg_prepared/pairs_manifest.csv  reg_prepared  reg_results

- **What it does:** per pair x mode (in-process tiling on/off), runs `bin/register.py`,
  `bin/estimate_feature_distances.py`, then `eval_tre` (loads the VALIS registrar pickle,
  warps the moving ground-truth landmarks).
- **Output:** `reg_results/eval_<pair>_<mode>.json` — three error views per pair/mode:
  true landmark **TRE/rTRE/um**, VALIS self-reported **rTRE**, feature-distance estimate.

### B3 — Aggregate

    python -m benchmarks.registration_eval.aggregate_eval \
        --eval-dir reg_results --out reg_eval.csv --agg-out reg_eval_agg.csv

- **Output:** `reg_eval.csv` (per pair/mode tidy rows) + `reg_eval_agg.csv` (per-mode
  **MMrTRE / AMrTRE**). `reg_eval.csv` is what `make_figures --reg-eval` and the notebook consume.

---

## C. The notebook (optional — interactive view of the same numbers)

> Not required. `make_figures` (A4) already writes `measurements.csv` +
> `resource_models.csv`, which are the canonical outputs to analyse yourself
> (e.g. in R). The notebook is just an interactive rendering of those same data.

    jupyter lab benchmarks/analysis/benchmark_analysis.ipynb

Edit the paths in the **setup cell** (`RESULTS_ROOT`, `RUN_PLAN`, `MANIFEST`, `REG_EVAL`)
to your outputs, then run top to bottom. Five sections: load -> regression -> optimal
config -> tiled-vs-classic accuracy -> paired sampling. Produces inline plots, the same
figures, and writes `conf/modules.optimized.config`.

## D. Put figures in the docs (optional)

    python -m benchmarks.analysis.export_docs_figures \
        --results-root bench_results --run-plan bench_run_plan.csv \
        --manifest bench_matrix/matrix_manifest.csv

- **Output:** `docs/assets/images/benchmarks/scaling_*.png`. Then uncomment the matching
  `<!-- ... -->` image slots in `docs/benchmarks.md` and run `mkdocs build`.

---

## Inputs -> outputs at a glance

| Step | You provide | You get |
|---|---|---|
| `generate_matrix.py` | 1 source image | matrix of OME-TIFFs + `matrix_manifest.csv` |
| `build_run_plan.py` | `sweep.yaml` | `run_plan.csv` |
| `run_sweep.sh` | manifest + run plan | per-run `trace.txt` + `input_sizes.csv` |
| `make_figures` | results + run plan + manifest | `measurements.csv` + `resource_models.csv` (tidy, for R) + `scaling_*.pdf/svg` + `modules.optimized.config` |
| `prepare_pairs.py` | `pairs.csv` (+ downloaded data) | per-pair input dirs + `pairs_manifest.csv` |
| `run_registration.sh` | pairs manifest | `eval_*.json` (3-way error x tiled/untiled) |
| `aggregate_eval` | eval JSONs | `reg_eval.csv` + `reg_eval_agg.csv` |
| notebook | all the above paths | inline figures + optimal config |

The fastest way to see every component work end-to-end **without any real data** is
`python -m pytest benchmarks/tests -q` — the suite drives the whole harness on tiny
synthetic fixtures.
