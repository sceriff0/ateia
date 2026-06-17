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
    df = pd.read_csv(input_sizes_csv)
    agg = df.groupby("process")["bytes"].sum().to_frame()
    agg["input_gb"] = agg["bytes"] / 2**30
    return agg


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
