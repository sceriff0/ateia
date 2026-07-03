"""Load + join Nextflow trace, size logs, run plan and matrix manifest into a tidy frame.

Generalises notebooks/resource_regression.ipynb:load_resource_data over a results
tree laid out by benchmarks/run_sweep.sh: <root>/<run_id>/trace/trace.txt and
<root>/<run_id>/out/size_logs/input_sizes.csv.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .parsing import parse_duration, parse_to_gb


def parse_trace(trace_txt) -> pd.DataFrame:
    df = pd.read_csv(trace_txt, sep="\t")
    out = pd.DataFrame({
        "process": df["process"],
        "tag": df.get("tag"),
        "status": df.get("status"),
        "exit": pd.to_numeric(df.get("exit"), errors="coerce"),
        "peak_rss_gb": df["peak_rss"].map(parse_to_gb),
        "peak_vmem_gb": df["peak_vmem"].map(parse_to_gb),
        "realtime_s": df["realtime"].map(parse_duration),
        "duration_s": df["duration"].map(parse_duration),
        "cpus": pd.to_numeric(df.get("cpus"), errors="coerce"),
    })
    return out


def parse_size_logs(input_sizes_csv) -> pd.DataFrame:
    """Per-process input size in GiB, averaged over the process's tasks.

    ``input_sizes.csv`` has one row per task (e.g. PREPROCESS emits one row per
    channel image). The trace also has one row per task, but there is no shared
    task-unique key to join on (both sides carry only ``process``/``sample_id``),
    so ``input_gb`` is broadcast to every trace row of a process. Averaging — not
    summing — makes that broadcast value the *per-task* input the memory model is
    fit against; summing would inflate x by the task count for multi-task
    processes and bias the regression. Single-task processes (one row) are
    unaffected: mean == the single value.
    """
    df = pd.read_csv(input_sizes_csv)
    agg = df.groupby("process")["bytes"].mean().to_frame()
    agg["input_gb"] = agg["bytes"] / 2**30
    return agg


def aggregate_repeats(runs_df: pd.DataFrame,
                      metrics=("peak_rss_gb", "peak_vmem_gb", "realtime_s",
                               "duration_s", "input_gb")) -> pd.DataFrame:
    """Collapse replicate runs into per-(process, config) mean / std / CV.

    Replicates share a ``config_id`` (from build_run_plan --repeats); this reports
    the spread across them — the variance estimate a single-shot (n=1) sweep can't
    give. Falls back to grouping on ``process`` alone if ``config_id`` is absent.
    For each metric: ``<m>_mean``, ``<m>_std`` (population, ddof=0), ``<m>_cv``
    (std/mean, NaN when mean==0), plus ``n_reps``. Descriptive param columns
    (varied_axis, target_px, n_channels) are carried through when present.
    """
    if runs_df.empty:
        return pd.DataFrame()
    keys = ["process"] + [k for k in ("config_id",) if k in runs_df.columns]
    carry = [c for c in ("varied_axis", "target_px", "n_channels") if c in runs_df.columns]
    metrics = [m for m in metrics if m in runs_df.columns]

    out_rows = []
    for key_vals, g in runs_df.groupby(keys):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        row = dict(zip(keys, key_vals))
        row["n_reps"] = len(g)
        for c in carry:
            row[c] = g[c].iloc[0]
        for m in metrics:
            vals = g[m].dropna()
            mean = float(vals.mean()) if len(vals) else float("nan")
            std = float(vals.std(ddof=0)) if len(vals) else float("nan")
            row[f"{m}_mean"] = mean
            row[f"{m}_std"] = std
            row[f"{m}_cv"] = (std / mean) if mean else float("nan")
        out_rows.append(row)
    cols = keys + ["n_reps"] + carry + [
        f"{m}_{s}" for m in metrics for s in ("mean", "std", "cv")]
    return pd.DataFrame(out_rows, columns=cols)


def load_runs(results_root, run_plan_csv, manifest_csv) -> pd.DataFrame:
    root = Path(results_root)
    plan = pd.read_csv(run_plan_csv)
    rows = []
    for _, prow in plan.iterrows():
        run_id = prow["run_id"]
        trace = root / run_id / "trace" / "trace.txt"
        sizes = root / run_id / "out" / "size_logs" / "input_sizes.csv"
        if not trace.exists():
            continue
        t = parse_trace(trace)
        if sizes.exists():
            s = parse_size_logs(sizes)
            t = t.merge(s["input_gb"], left_on="process", right_index=True, how="left")
        else:
            t["input_gb"] = float("nan")
        for col in plan.columns:
            t[col] = prow[col]
        rows.append(t)
    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return df
