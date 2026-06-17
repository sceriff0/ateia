# Mirage Benchmarking

Quantifies resource usage vs input size + pipeline parameters, and registration
accuracy (classic vs tiled) against landmark ground truth. See the design spec:
`docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md`.

## Resource sweep

1. Generate the input matrix from your own source image (use `--paired` so each
   cell with n_channels>=2 also gets a moving panel with distinct channel names,
   making registration axes meaningful):

       python benchmarks/generate_matrix.py --source /path/to/source.ome.tif --outdir bench_matrix --paired

2. Expand the sweep into a run plan:

       python benchmarks/build_run_plan.py --sweep benchmarks/configs/sweep.yaml --out bench_run_plan.csv

3. Launch the sweep (one pipeline run per row; per-run trace + size logs isolated):

       benchmarks/run_sweep.sh bench_run_plan.csv bench_matrix/matrix_manifest.csv bench_results

Each run writes `bench_results/<run_id>/trace/trace.txt` and
`bench_results/<run_id>/out/size_logs/input_sizes.csv`, joined by the analysis notebook (Plan 3).

> `run_sweep.sh` requires bash 4+ (associative arrays). On macOS, install a modern bash
> (`brew install bash`) and invoke it explicitly if `/bin/bash` is 3.x.

> **Note — registration in the resource sweep.** When `--paired` is used with
> `generate_matrix.py`, `run_sweep.sh` emits a reference+moving panel per patient
> (distinct channel names: reference uses `DAPI|ch1|…`, moving uses `DAPI|m1|…`),
> so VALIS registration actually runs and registration axes are exercised. Without
> `--paired`, a single-slide passthrough is emitted and registration axes are no-ops.

## Registration accuracy (Plan 2)

ANHIR is account-gated; ACROBAT landmarks are challenge-gated. Download with your own
account, then point `benchmarks/registration_eval/prepare_pairs.py` at the local data dir.
Steps live in `benchmarks/registration_eval/README.md`.
