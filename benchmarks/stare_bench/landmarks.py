"""Landmark model and target-registration-error (TRE) math.

Pure and dependency-free (numpy only) so it is fully unit-testable.
TRE conventions follow ANHIR (rTRE = TRE / image diagonal) and ACROBAT (µm).

Relocated from benchmarks/registration_eval/ when the ANHIR/ACROBAT landmark harness
was deleted; these primitives survived because stare_bench scores against them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class LandmarkPair:
    """Matched landmarks for one image pair, in pixel coordinates.

    moving_xy: (N, 2) points in the MOVING image frame (to be warped).
    target_xy: (N, 2) corresponding points in the TARGET/reference frame.
    """
    moving_xy: np.ndarray
    target_xy: np.ndarray
    pair_id: str = ""
    meta: dict = field(default_factory=dict)


def per_landmark_tre(warped_xy, target_xy) -> np.ndarray:
    """Euclidean distance (pixels) between each warped point and its target."""
    a = np.asarray(warped_xy, dtype=float)
    b = np.asarray(target_xy, dtype=float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 2:
        raise ValueError(f"expected matching (N,2) arrays, got {a.shape} and {b.shape}")
    return np.sqrt(((a - b) ** 2).sum(axis=1))


def image_diagonal(width: int, height: int) -> float:
    """Image diagonal in pixels (rTRE denominator)."""
    return float(np.hypot(width, height))


def summarize(tre_px, diagonal: float, pixel_size_um: float | None = None) -> dict:
    """Reduce per-landmark TRE to summary stats. rTRE = TRE / diagonal."""
    tre = np.asarray(tre_px, dtype=float)
    diag = float(diagonal)
    out = {
        "n": int(tre.size),
        "median_px": float(np.median(tre)),
        "mean_px": float(np.mean(tre)),
        "p90_px": float(np.percentile(tre, 90)),
        "median_rtre": float(np.median(tre) / diag),
        "mean_rtre": float(np.mean(tre / diag)),
    }
    if pixel_size_um is not None:
        out["median_um"] = float(np.median(tre) * pixel_size_um)
        out["p90_um"] = float(np.percentile(tre, 90) * pixel_size_um)
    return out
