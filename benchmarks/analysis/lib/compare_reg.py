"""Classic vs distributed registration — end-result comparison for the benchmark report.

The sweep runs the registration stage two ways at the baseline pair (the ``reg_distributed_tiling``
OFAT axis): classic ``REGISTER`` (monolithic, JVM held through the whole stage) vs the distributed
``REG_*`` fan-out (JVM-free non-rigid, lower per-node RAM ceiling). The default distributed path is
SEPARATED whole-image non-rigid, which is BIT-IDENTICAL to classic (proven by
``tests/integration/compare_classic_vs_distributed.py`` — a code-level gate, not a benchmark one). So
this section is about the *cost* delta of an equivalent result: does distributed actually buy the RAM
win the design exists for, and what does it cost in wall/compute time.

``compare_classic_vs_distributed(runs_df)`` takes the per-(process, run) frame from ``load.load_runs``
and returns one row per (classic config, distributed config) pair with the registration-stage peak RSS
and total realtime for each side plus their deltas/ratios.
"""
from __future__ import annotations

import pandas as pd

# Registration-stage process leaf names (trace 'process' is a colon path, e.g.
# MIRAGE:REGISTRATION:VALIS_ADAPTER:REGISTER — match on the leaf).
CLASSIC_LEAVES = {"REGISTER"}
DISTRIBUTED_LEAVES = {
    "REG_PREP", "REG_TILE", "REG_NONRIGID", "REG_MICRO_PREP",
    "REG_FINALIZE", "REG_FINALIZE_FIELD", "REG_FINALIZE_MICRO",
    "REG_WARP_REF", "REG_ESTIMATE",
}


def _leaf(process: str) -> str:
    return str(process).split(":")[-1]


def _truthy(v) -> bool:
    """reg_distributed_tiling may arrive as bool, 'true'/'false' (Nextflow), or 0/1."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _stage_metrics(run_rows: pd.DataFrame) -> dict:
    """Registration-stage cost for one run: peak RSS = MAX across the stage's processes (the per-node
    ceiling the design targets), total realtime = SUM (total compute the stage cost)."""
    return {
        "reg_peak_rss_gb": float(run_rows["peak_rss_gb"].max()),
        "reg_total_realtime_s": float(run_rows["realtime_s"].sum()),
        "n_processes": int(len(run_rows)),
    }


def compare_classic_vs_distributed(runs_df: pd.DataFrame) -> pd.DataFrame:
    """Pair classic vs distributed configs (identical except reg_distributed_tiling) and report the
    registration-stage cost delta. Returns an empty frame if the sweep has no distributed runs.

    Columns: config pairing keys + reg_peak_rss_gb_{classic,distributed}, reg_total_realtime_s_{...},
    peak_rss_ratio (distributed/classic), realtime_ratio, and rss_saving_gb (classic-distributed).
    """
    if runs_df.empty or "reg_distributed_tiling" not in runs_df.columns:
        return pd.DataFrame()

    df = runs_df.copy()
    df["_leaf"] = df["process"].map(_leaf)
    df["_dist"] = df["reg_distributed_tiling"].map(_truthy)

    # Cell = the input point a classic and distributed run share (they differ only in the path taken).
    # Pair per cell so a size-crossed distributed_grid compares against the SAME-size classic run.
    cell_keys = [k for k in ("target_px", "n_channels", "n_register_images") if k in df.columns]

    # Reduce each run to its registration-stage metrics.
    group_keys = [k for k in ("config_id", "run_id", "rep") if k in df.columns] or ["run_id"]
    per_run = []
    for keyvals, g in df.groupby(group_keys):
        is_dist = bool(g["_dist"].iloc[0])
        leaves = DISTRIBUTED_LEAVES if is_dist else CLASSIC_LEAVES
        stage = g[g["_leaf"].isin(leaves)]
        if stage.empty:
            continue
        row = {k: g[k].iloc[0] for k in cell_keys}
        row["is_distributed"] = is_dist
        row.update(_stage_metrics(stage))
        per_run.append(row)
    if not per_run:
        return pd.DataFrame()
    runs = pd.DataFrame(per_run)

    # Average replicates within (cell, path).
    metrics = ["reg_peak_rss_gb", "reg_total_realtime_s"]
    agg = runs.groupby(cell_keys + ["is_distributed"])[metrics].mean().reset_index()

    classic = agg[~agg["is_distributed"]]
    dist = agg[agg["is_distributed"]]
    if classic.empty or dist.empty:
        return pd.DataFrame()

    # Inner-join classic vs distributed on the cell keys — only cells measured BOTH ways are compared.
    merged = classic.merge(dist, on=cell_keys, suffixes=("_classic", "_distributed"))
    if merged.empty:
        return pd.DataFrame()
    rss_c, rss_d = merged["reg_peak_rss_gb_classic"], merged["reg_peak_rss_gb_distributed"]
    rt_c, rt_d = merged["reg_total_realtime_s_classic"], merged["reg_total_realtime_s_distributed"]
    merged["peak_rss_ratio"] = rss_d / rss_c.where(rss_c != 0)
    merged["realtime_ratio"] = rt_d / rt_c.where(rt_c != 0)
    merged["rss_saving_gb"] = rss_c - rss_d
    drop = [c for c in ("is_distributed_classic", "is_distributed_distributed") if c in merged]
    return merged.drop(columns=drop).sort_values(cell_keys).reset_index(drop=True)
