"""Aggregate per-pair/mode eval_*.json into a tidy CSV for the analysis notebook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _flatten(rec: dict) -> dict:
    tre = rec.get("true_tre") or {}
    return {
        "pair_id": rec.get("pair_id"),
        "mode": rec.get("mode"),
        "true_median_px": tre.get("median_px"),
        "true_mean_px": tre.get("mean_px"),
        "true_p90_px": tre.get("p90_px"),
        "true_median_rtre": tre.get("median_rtre"),
        "true_median_um": tre.get("median_um"),
        "true_p90_um": tre.get("p90_um"),
        "valis_rtre": rec.get("valis_rtre"),
    }


def aggregate(eval_dir):
    """Return (per_pair_df, per_mode_agg_df). per_mode adds ANHIR MMrTRE/AMrTRE."""
    rows = [_flatten(json.loads(p.read_text())) for p in sorted(Path(eval_dir).glob("*.json"))]
    df = pd.DataFrame(rows)
    agg = (
        df.groupby("mode")["true_median_rtre"]
        .agg(MMrTRE="median", AMrTRE="mean")
        .reset_index()
    )
    return df, agg


def main():
    ap = argparse.ArgumentParser(description="Aggregate registration eval JSONs.")
    ap.add_argument("--eval-dir", required=True)
    ap.add_argument("--out", required=True, help="output CSV (per-pair)")
    ap.add_argument("--agg-out", default=None, help="optional per-mode aggregate CSV")
    a = ap.parse_args()
    df, agg = aggregate(a.eval_dir)
    df.to_csv(a.out, index=False)
    if a.agg_out:
        agg.to_csv(a.agg_out, index=False)
    print(f"Wrote {len(df)} rows to {a.out}")


if __name__ == "__main__":
    main()
