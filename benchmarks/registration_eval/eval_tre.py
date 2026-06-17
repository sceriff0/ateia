"""Evaluate registration accuracy for one image pair / mode.

Loads a VALIS registrar pickle, warps the moving ground-truth landmarks into the
target frame, and merges three estimates: true landmark TRE, VALIS self-rTRE,
and the feature-distance estimate. VALIS import is lazy + injectable so the pure
logic is testable without VALIS.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .landmarks import LandmarkPair, image_diagonal, per_landmark_tre, summarize


def default_loader(pickle_path):
    from valis_lib import registration  # lazy: only needed at runtime
    return registration.load_registrar(str(pickle_path))


def warp_moving_landmarks(registrar, slide_name: str, moving_xy, non_rigid: bool = True) -> np.ndarray:
    slide = registrar.slide_dict[slide_name]
    return np.asarray(slide.warp_xy(np.asarray(moving_xy, dtype=float), non_rigid=non_rigid), dtype=float)


def evaluate_pair(pair: LandmarkPair, pickle_path, slide_name: str, width: int, height: int,
                  pixel_size_um=None, loader=default_loader, non_rigid: bool = True):
    registrar = loader(pickle_path)
    warped = warp_moving_landmarks(registrar, slide_name, pair.moving_xy, non_rigid=non_rigid)
    tre = per_landmark_tre(warped, pair.target_xy)
    return summarize(tre, image_diagonal(width, height), pixel_size_um), tre


def read_valis_rtre(summary_csv):
    """VALIS self-reported rTRE: prefer non_rigid_rTRE, fall back to rigid_rTRE."""
    df = pd.read_csv(summary_csv)
    for col in ("non_rigid_rTRE", "rigid_rTRE"):
        if col in df.columns:
            val = pd.to_numeric(df[col], errors="coerce").dropna()
            if not val.empty:
                return float(val.iloc[0])
    return None


def read_feature_estimate(feature_json):
    data = json.loads(Path(feature_json).read_text())
    return data.get("after_registration", {}).get("feature_distances")


def build_eval_record(pair_id, mode, tre_summary, valis_rtre=None, feature_estimate=None) -> dict:
    return {
        "pair_id": pair_id,
        "mode": mode,
        "true_tre": tre_summary,
        "valis_rtre": valis_rtre,
        "feature_estimate": feature_estimate,
    }


def main():
    ap = argparse.ArgumentParser(description="Evaluate registration TRE for one pair/mode.")
    ap.add_argument("--pickle", required=True, help="VALIS registrar pickle")
    ap.add_argument("--slide-name", required=True, help="moving slide name in registrar.slide_dict")
    ap.add_argument("--source-landmarks", required=True)
    ap.add_argument("--target-landmarks", required=True)
    ap.add_argument("--adapter", choices=["anhir"], default="anhir",
                    help="landmark loader (acrobat pairs are pre-split by prepare_pairs)")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--pixel-size-um", type=float, default=None)
    ap.add_argument("--valis-summary", default=None)
    ap.add_argument("--feature-json", default=None)
    ap.add_argument("--mode", required=True, choices=["tiled", "untiled"])
    ap.add_argument("--pair-id", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from .adapters import anhir
    pair = anhir.load_pair(a.source_landmarks, a.target_landmarks, pair_id=a.pair_id)
    tre_summary, _ = evaluate_pair(pair, a.pickle, a.slide_name, a.width, a.height, a.pixel_size_um)
    record = build_eval_record(
        a.pair_id, a.mode, tre_summary,
        valis_rtre=read_valis_rtre(a.valis_summary) if a.valis_summary else None,
        feature_estimate=read_feature_estimate(a.feature_json) if a.feature_json else None,
    )
    Path(a.out).write_text(json.dumps(record, indent=2))
    print(f"Wrote {a.out}")


if __name__ == "__main__":
    main()
