"""Evaluate registration accuracy for one image pair / method.

Warps the moving ground-truth landmarks into the target frame and merges the
landmark TRE with every *method-native* accuracy signal the pipeline already
emits for that run, so the ground truth and the self-reported numbers sit in one
record and can be compared directly:

  * ``true_tre``   -- landmark TRE/rTRE/um. The only ground-truth signal.
  * ``valis_rtre`` -- VALIS's own ``error_df`` rTRE (method='valis' runs).
  * ``stare_tre``  -- STARE's intrinsic ``_tre.json`` (method='tiled' runs).
  * ``seg_qc``     -- reg_qc=2 segmentation-overlap dice + centroid displacement,
                      the pipeline's landmark-free accuracy signal, when the run
                      produced a ``*_seg_qc.json``.

Both warpers are lazy + injectable so the pure logic is testable without VALIS
and without the STARE runtime.

RELOCATED from benchmarks/registration_eval/eval_tre.py when the ANHIR/ACROBAT landmark
harness was deleted. These are the landmark-TRE PRIMITIVES, and they survived that
deletion because stare_bench depends on them: cli.py imports ``default_loader``, and
generate.py's synthetic pairs are built so ``evaluate_pair`` scores them unchanged.

The module's ``main()`` did NOT survive -- it was the deleted harness's per-pair CLI and
imported ``.adapters.anhir``, which no longer exists. Nothing in this package invoked it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .landmarks import LandmarkPair, image_diagonal, per_landmark_tre, summarize

METHODS = ("valis", "tiled")


def default_loader(pickle_path):
    from valis_lib import registration  # lazy: only needed at runtime
    return registration.load_registrar(str(pickle_path))


def warp_moving_landmarks(registrar, slide_name: str, moving_xy, non_rigid: bool = True) -> np.ndarray:
    slide = registrar.slide_dict[slide_name]
    return np.asarray(slide.warp_xy(np.asarray(moving_xy, dtype=float), non_rigid=non_rigid), dtype=float)


def default_tiled_loader(manifest_path):
    """Build STARE's ``warp(slide, xy, stage)`` from a transform manifest.

    Same builder WARP_SEG_QC_TILED uses, so the landmark TRE here is measured
    through exactly the transform the pipeline's own reg_qc=2 scores.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "utils"))
    from tiled_stage_warp import make_warper  # lazy: pulls numpy-only STARE code

    return make_warper(json.loads(Path(manifest_path).read_text()))


def warp_moving_landmarks_tiled(warp, slide_name: str, moving_xy, non_rigid: bool = True) -> np.ndarray:
    """STARE analogue of ``warp_moving_landmarks``.

    ``non_rigid=True`` selects the mesh-refined stage; False stops at the rigid
    stage, mirroring the VALIS ``non_rigid`` switch so the two methods are
    evaluated at comparable points.
    """
    stage = "refined" if non_rigid else "rigid"
    return np.asarray(warp(slide_name, np.asarray(moving_xy, dtype=float), stage), dtype=float)


def evaluate_pair(pair: LandmarkPair, transform_path, slide_name: str, width: int, height: int,
                  pixel_size_um=None, loader=None, non_rigid: bool = True, method: str = "valis"):
    """Landmark TRE for one pair, through either method's transform.

    ``transform_path`` is a VALIS registrar pickle for method='valis' and a STARE
    transform manifest JSON for method='tiled'.
    """
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")
    if method == "tiled":
        warp = (loader or default_tiled_loader)(transform_path)
        warped = warp_moving_landmarks_tiled(warp, slide_name, pair.moving_xy, non_rigid=non_rigid)
    else:
        registrar = (loader or default_loader)(transform_path)
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


def read_stare_tre(tre_json):
    """STARE's intrinsic TRE, flattened to the headline numbers.

    ``residual_after_px`` is the post-mesh residual (STARE's analogue of VALIS's
    non-rigid error) and is absent when no tile was refined, so it stays None
    rather than silently falling back to the rigid number.
    """
    d = json.loads(Path(tre_json).read_text())

    def pct(block, key):
        b = d.get(block)
        return b.get(key) if isinstance(b, dict) else None

    return {
        "coarse_tre_px": d.get("coarse_tre_px"),
        "n_tiles": d.get("n_tiles"),
        "mesh_refined": bool(d.get("mesh_refined")),
        "rigid_p50_px": pct("rigid_tre_px", "p50"),
        "rigid_p90_px": pct("rigid_tre_px", "p90"),
        "final_p50_px": pct("residual_after_px", "p50"),
        "final_p90_px": pct("residual_after_px", "p90"),
    }


def read_seg_qc(seg_qc_json):
    """reg_qc=2 segmentation-overlap accuracy for the FINAL transform stage.

    bin/warp_seg_qc.py scores every stage; the last entry of ``stage_order`` is
    the fully-registered one, which is what compares against the landmark TRE.
    Falls back to the last key of ``stages`` when ``stage_order`` is absent.
    """
    d = json.loads(Path(seg_qc_json).read_text())
    stages = d.get("stages") or {}
    if not stages:
        return None
    order = d.get("stage_order") or list(stages)
    final = stages.get(order[-1]) or {}
    return {
        "stage": order[-1],
        "dice_matched": final.get("dice_matched"),
        "iou_mean": final.get("iou_mean"),
        "median_displacement_um": final.get("median_displacement_um"),
        "median_displacement_px": final.get("median_displacement_px"),
        "n_pairs": (d.get("matching") or {}).get("n_pairs"),
    }


def build_eval_record(pair_id, mode, tre_summary, valis_rtre=None, stare_tre=None,
                      seg_qc=None) -> dict:
    return {
        "pair_id": pair_id,
        "mode": mode,
        "true_tre": tre_summary,
        "valis_rtre": valis_rtre,
        "stare_tre": stare_tre,
        "seg_qc": seg_qc,
    }
