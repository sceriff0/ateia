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


def run(results_root, run_plan_csv, manifest_csv, reg_eval_csv, outdir, formats=("pdf", "svg")) -> dict:
    outdir = Path(outdir)
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    plotting.set_paper_theme()

    runs_df = load.load_runs(results_root, run_plan_csv, manifest_csv)
    models = regress.fit_per_process(runs_df, predictor="input_gb", target="peak_rss_gb")

    # per-process memory scaling figures
    for proc, g in runs_df.groupby("process"):
        sub = g[["input_gb", "peak_rss_gb"]].dropna()
        if sub.empty:
            continue
        m = models[proc]
        fig = plotting.scatter_with_fit(
            sub["input_gb"], sub["peak_rss_gb"], m["slope"], m["intercept"],
            xlabel="input (GiB)", ylabel="peak RSS (GiB)", title=proc)
        plotting.save_fig(fig, figdir / f"scaling_{proc}", formats=formats)

    emit_config.write_optimized_config(models, outdir / "modules.optimized.config")
    return {"runs_df": runs_df, "models": models, "outdir": outdir}


def main():
    ap = argparse.ArgumentParser(description="Benchmark analysis: figures + optimal config.")
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--run-plan", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--reg-eval", default=None)
    ap.add_argument("--outdir", default="benchmarks/analysis")
    a = ap.parse_args()
    res = run(a.results_root, a.run_plan, a.manifest, a.reg_eval, a.outdir)
    print(f"Wrote {res['outdir']}/modules.optimized.config and figures for "
          f"{len(res['models'])} processes")


if __name__ == "__main__":
    main()
