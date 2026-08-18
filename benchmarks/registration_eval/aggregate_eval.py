"""Aggregate per-pair/mode eval_*.json into a tidy CSV for the analysis notebook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _flatten(rec: dict) -> dict:
    """One tidy row per (pair, mode): ground truth first, then each method-native signal.

    The method-native columns are sparse by construction — a VALIS run has no
    ``stare_*`` and vice versa, and ``seg_*`` only appears when reg_qc=2 ran — so
    they stay NaN rather than being dropped, keeping one stable column set.
    """
    tre = rec.get("true_tre") or {}
    stare = rec.get("stare_tre") or {}
    seg = rec.get("seg_qc") or {}
    return {
        "pair_id": rec.get("pair_id"),
        "mode": rec.get("mode"),
        # ── ground truth (landmarks) ──
        "true_median_px": tre.get("median_px"),
        "true_mean_px": tre.get("mean_px"),
        "true_p90_px": tre.get("p90_px"),
        "true_median_rtre": tre.get("median_rtre"),
        "true_median_um": tre.get("median_um"),
        "true_p90_um": tre.get("p90_um"),
        # ── method-native: VALIS error_df ──
        "valis_rtre": rec.get("valis_rtre"),
        # ── method-native: STARE intrinsic TRE ──
        "stare_coarse_tre_px": stare.get("coarse_tre_px"),
        "stare_rigid_p50_px": stare.get("rigid_p50_px"),
        "stare_final_p50_px": stare.get("final_p50_px"),
        "stare_mesh_refined": stare.get("mesh_refined"),
        # ── landmark-free: reg_qc=2 segmentation overlap ──
        "seg_stage": seg.get("stage"),
        "seg_dice_matched": seg.get("dice_matched"),
        "seg_median_displacement_um": seg.get("median_displacement_um"),
        "seg_n_pairs": seg.get("n_pairs"),
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
