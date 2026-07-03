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

    # Reduce each run to its registration-stage metrics (averaged over repeats via config_id).
    group_keys = [k for k in ("config_id", "run_id", "rep") if k in df.columns] or ["run_id"]
    per_run = []
    for keyvals, g in df.groupby(group_keys):
        is_dist = bool(g["_dist"].iloc[0])
        leaves = DISTRIBUTED_LEAVES if is_dist else CLASSIC_LEAVES
        stage = g[g["_leaf"].isin(leaves)]
        if stage.empty:
            continue
        row = dict(zip(group_keys, keyvals if isinstance(keyvals, tuple) else (keyvals,)))
        row["is_distributed"] = is_dist
        row.update(_stage_metrics(stage))
        per_run.append(row)
    if not per_run:
        return pd.DataFrame()
    runs = pd.DataFrame(per_run)

    # Average replicates within a config.
    cfg_key = "config_id" if "config_id" in runs.columns else "run_id"
    agg = (runs.groupby([cfg_key, "is_distributed"])[["reg_peak_rss_gb", "reg_total_realtime_s"]]
           .mean().reset_index())

    classic = agg[~agg["is_distributed"]].set_index(cfg_key)
    dist = agg[agg["is_distributed"]].set_index(cfg_key)
    if classic.empty or dist.empty:
        return pd.DataFrame()

    # There is one classic baseline config and (per OFAT) one distributed config that differ ONLY in
    # reg_distributed_tiling — pair every distributed config against the single classic baseline.
    base = classic.iloc[0]
    out = []
    for cid, d in dist.iterrows():
        out.append({
            "classic_config": classic.index[0],
            "distributed_config": cid,
            "reg_peak_rss_gb_classic": base["reg_peak_rss_gb"],
            "reg_peak_rss_gb_distributed": d["reg_peak_rss_gb"],
            "reg_total_realtime_s_classic": base["reg_total_realtime_s"],
            "reg_total_realtime_s_distributed": d["reg_total_realtime_s"],
            "peak_rss_ratio": (d["reg_peak_rss_gb"] / base["reg_peak_rss_gb"]
                               if base["reg_peak_rss_gb"] else float("nan")),
            "realtime_ratio": (d["reg_total_realtime_s"] / base["reg_total_realtime_s"]
                               if base["reg_total_realtime_s"] else float("nan")),
            "rss_saving_gb": base["reg_peak_rss_gb"] - d["reg_peak_rss_gb"],
        })
    return pd.DataFrame(out)
