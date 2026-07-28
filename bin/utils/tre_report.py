"""STARE's intrinsic TRE report — the method's own Target Registration Error, à la VALIS.

Built from the same features/correlations the registration itself uses, so it needs no external
ground truth (exactly what makes VALIS's ``error_df`` intrinsic). Two entry points share this
builder so the ``_tre.json`` has one shape:

  * ``coarse_tre_px``     — RMS residual of the ORB matches after the global rigid M0 (rigid-stage
                            TRE; directly comparable to VALIS's rigid error).
  * ``rigid_tre_px``      — percentiles of the per-tile rigid-stage misalignment, plus the full
                            per-tile ``tiles`` list = a *spatial* TRE heatmap VALIS does not give.
  * ``residual_after_px`` — (when measured) percentiles of the per-tile residual AFTER the mesh is
                            applied: STARE's post-registration final-accuracy number, the analogue
                            of VALIS's non-rigid error.

Pure NumPy.
"""

from __future__ import annotations

import numpy as np

__all__ = ["build_tre_report"]


def _pct(values):
    a = np.asarray(list(values), dtype=float)
    if a.size == 0:
        return {"mean": None, "p50": None, "p90": None, "max": None}
    return {
        "mean": float(a.mean()),
        "p50": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


def build_tre_report(coarse_tre_px, n_inliers, tile_records, mesh_refined):
    """Assemble the intrinsic-TRE report.

    ``tile_records`` is a list of per-tile dicts carrying at least ``ix, iy, cx, cy, tre_rigid``;
    if every record also has ``tre_after`` the post-refinement residual is summarised too.
    """
    report = {
        "coarse_tre_px": float(coarse_tre_px),
        "n_inliers": int(n_inliers),
        "n_tiles": len(tile_records),
        "mesh_refined": bool(mesh_refined),
        "rigid_tre_px": _pct(t["tre_rigid"] for t in tile_records),
        "tiles": tile_records,  # spatial heatmap: one entry per tile
    }
    if tile_records and all("tre_after" in t for t in tile_records):
        report["residual_after_px"] = _pct(t["tre_after"] for t in tile_records)
    return report
