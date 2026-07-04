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

import pandas as pd

from .lib import compare_reg, emit_config, load, plotting, quality, regress

# Only these runs change input_gb: the scaling grid (size x channels), the
# registration grid (size x n_register_images — REGISTER's input is the SUM of all
# rounds, and merged channels grow with round count, so this genuinely varies input),
# the baseline cell, and — for back-compat with older OFAT plans — the target_px /
# n_channels axes. Every OTHER axis (memory_mode, seg_gpu, ...) holds input fixed at
# the baseline cell, so pooling them into a peak_rss_gb ~ input_gb regression piles
# many points at one x — inflating sigma, depressing r2, perturbing the intercept.
# The memory-scaling fit must use only the size-varying runs.
SIZE_VARYING_AXES = {"baseline", "scaling_grid", "registration_grid",
                     "target_px", "n_channels"}


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

    runs_all = load.load_runs(results_root, run_plan_csv, manifest_csv)
    # measurements.csv keeps EVERY row (incl. failed processes, with status/exit) — lossless. But the
    # fits/aggregates/comparisons use only SUCCESSFUL processes: a failed CellSAM/timeout has bogus
    # peak_rss + realtime that would corrupt the means and the scaling model.
    runs_df = load.only_successful(runs_all)
    n_dropped = len(runs_all) - len(runs_df)
    if n_dropped:
        print(f"[analysis] excluded {n_dropped} failed/incomplete process rows from the aggregates "
              f"(measurements.csv keeps them)")
    # Fit the memory model on the size-varying runs only (avoids the confound).
    scaling_df = _size_varying(runs_df)
    models = regress.fit_per_process(scaling_df, predictor="input_gb", target="peak_rss_gb")

    # Tidy CSVs for downstream analysis (R, etc.) — these are the primary data
    # artifacts; figures and the notebook are optional views of the same numbers.
    measurements_csv = outdir / "measurements.csv"
    models_csv = outdir / "resource_models.csv"
    stats_csv = outdir / "resource_stats.csv"
    runs_all.to_csv(measurements_csv, index=False)   # lossless (all rows, incl. failures)
    regress.models_to_frame(models).to_csv(models_csv, index=False)
    # per-config replicate variance (mean/std/CV across repeats) — empty CSV with
    # no runs, but always written so the artifact set is stable.
    stats_df = load.aggregate_repeats(runs_df)
    stats_df.to_csv(stats_csv, index=False)

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

    # Classic vs distributed registration — the reg_distributed_tiling axis, if swept. Reports the
    # registration-stage peak-RSS and compute-time delta (the equivalent-result cost comparison; the
    # bit-identical guarantee itself is a code-level gate, tests/integration/compare_classic_vs_distributed.py).
    compare_csv = outdir / "classic_vs_distributed_registration.csv"
    compare_df = compare_reg.compare_classic_vs_distributed(runs_df)
    compare_df.to_csv(compare_csv, index=False)  # always written (may be empty) for a stable artifact set

    # ── QUALITY + COST (the result-quality the trace ignores + derived cost). All best-effort and
    #    robust to missing/failed runs, so a CellSAM run that OOMs simply contributes no rows. ──
    # Per-run cost (cpu/gpu-hours, wall-clock, bottleneck) — from the trace, always available.
    quality.run_cost_summary(runs_df).to_csv(outdir / "run_cost.csv", index=False)
    # Per-run quality: registration accuracy (feature-error JSON) + segmentation cell count (mask max),
    # joined to the swept params so the analysis/plots can relate accuracy to config + cost.
    per_run = runs_df.drop_duplicates("run_id")[
        [c for c in ("run_id", "varied_axis", "target_px", "n_channels", "n_register_images",
                     "memory_mode", "skip_micro_registration", "seg_method", "reg_distributed_tiling",
                     "reg_dist_force_tiling")
         if c in runs_df.columns]] if not runs_df.empty else pd.DataFrame(columns=["run_id"])
    reg_err = quality.harvest_registration_error(results_root, run_plan_csv)
    try:
        seg_cnt = quality.harvest_segmentation_counts(results_root, run_plan_csv)  # reads masks (I/O)
    except Exception:
        seg_cnt = pd.DataFrame(columns=["run_id"])
    quality_df = per_run
    for extra in (reg_err, seg_cnt):
        if not extra.empty:
            quality_df = quality_df.merge(extra, on="run_id", how="left")
    quality_df.to_csv(outdir / "quality.csv", index=False)
    # Segmentation cross-method agreement (pairwise mask IoU + cell-count ratio) — best-effort.
    try:
        seg_agree = quality.segmentation_agreement(results_root, run_plan_csv)
    except Exception:
        seg_agree = pd.DataFrame()
    seg_agree.to_csv(outdir / "segmentation_agreement.csv", index=False)

    return {"runs_df": runs_df, "models": models, "outdir": outdir,
            "measurements_csv": measurements_csv, "models_csv": models_csv,
            "stats_csv": stats_csv, "compare_csv": compare_csv}


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
          f"{res['stats_csv']} (per-config variance), "
          f"{res['outdir']}/modules.optimized.config, and figures")


if __name__ == "__main__":
    main()
