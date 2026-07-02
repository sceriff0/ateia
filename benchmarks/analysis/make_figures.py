"""Headless end-to-end benchmark analysis: parse -> regress -> plot -> emit config.

This is the reproducible artifact the notebook mirrors. Run:
  python -m benchmarks.analysis.make_figures --results-root <dir> \
      --run-plan run_plan.csv --manifest matrix_manifest.csv \
      --reg-eval reg_eval.csv --outdir benchmarks/analysis/figures
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

from .lib import emit_config, load, plotting, regress

# Only these OFAT axes change the input image size (and therefore input_gb).
# Every other axis (memory_mode, seg_gpu, preproc_n_iter, ...) holds input fixed
# at the baseline cell, so pooling them into a peak_rss_gb ~ input_gb regression
# piles many points at one x — inflating sigma, depressing r2, and perturbing the
# intercept. The memory-scaling fit must use only the size-varying runs.
SIZE_VARYING_AXES = {"baseline", "target_px", "n_channels"}


def _size_varying(runs_df):
    """Rows whose varied_axis changes input size; fall back to all rows if the
    column is absent (older run plans)."""
    if "varied_axis" not in runs_df.columns:
        return runs_df
    return runs_df[runs_df["varied_axis"].isin(SIZE_VARYING_AXES)]


def run(results_root, run_plan_csv, manifest_csv, reg_eval_csv, outdir, formats=("pdf", "svg")) -> dict:
    outdir = Path(outdir)
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    plotting.set_paper_theme()

    runs_df = load.load_runs(results_root, run_plan_csv, manifest_csv)
    # Fit the memory model on the size-varying runs only (avoids the confound);
    # runs_df keeps every run so measurements.csv loses nothing.
    scaling_df = _size_varying(runs_df)
    models = regress.fit_per_process(scaling_df, predictor="input_gb", target="peak_rss_gb")

    # Tidy CSVs for downstream analysis (R, etc.) — these are the primary data
    # artifacts; figures and the notebook are optional views of the same numbers.
    measurements_csv = outdir / "measurements.csv"
    models_csv = outdir / "resource_models.csv"
    runs_df.to_csv(measurements_csv, index=False)
    regress.models_to_frame(models).to_csv(models_csv, index=False)

    # per-process memory scaling figures (size-varying runs only, matching the fit)
    for proc, g in scaling_df.groupby("process"):
        sub = g[["input_gb", "peak_rss_gb"]].dropna()
        if sub.empty:
            continue
        m = models[proc]
        fig = plotting.scatter_with_fit(
            sub["input_gb"], sub["peak_rss_gb"], m["slope"], m["intercept"],
            xlabel="input (GiB)", ylabel="peak RSS (GiB)", title=proc)
        plotting.save_fig(fig, figdir / f"scaling_{proc}", formats=formats)

    emit_config.write_optimized_config(models, outdir / "modules.optimized.config")
    return {"runs_df": runs_df, "models": models, "outdir": outdir,
            "measurements_csv": measurements_csv, "models_csv": models_csv}


def main():
    ap = argparse.ArgumentParser(description="Benchmark analysis: figures + optimal config.")
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--run-plan", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--reg-eval", default=None)
    ap.add_argument("--outdir", default="benchmarks/analysis")
    a = ap.parse_args()
    res = run(a.results_root, a.run_plan, a.manifest, a.reg_eval, a.outdir)
    print(f"Wrote {res['measurements_csv']} ({len(res['runs_df'])} rows), "
          f"{res['models_csv']} ({len(res['models'])} processes), "
          f"{res['outdir']}/modules.optimized.config, and figures")


if __name__ == "__main__":
    main()
