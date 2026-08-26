"""Endpoint error against the known field -- the metric landmarks cannot give.

Ground truth exists at EVERY pixel here, not at a few hundred hand-placed
points, which is the whole reason synthetic data beats ANHIR/ACROBAT for this
problem. Manual landmarks carry a ~115 um annotation floor; this has none.

REDUCTION IS STREAMING, tile by tile. A gigapixel error array would be ~20 GB,
and a benchmark that blows the memory budget while measuring compliance with a
memory budget is not a benchmark. Peak memory here is bounded by ``tile``.
"""

from __future__ import annotations

import numpy as np

__all__ = ["epe"]


def epe(field, predict, shape, tile=1024, subsample=1):
    """Endpoint error of ``predict`` against ``field`` over a ``shape`` raster."""
    h, w = int(shape[0]), int(shape[1])
    step = max(1, int(subsample))
    # Percentiles need the sample, but only the per-point error scalar -- one
    # float per sampled pixel, not the two-component field.
    errors = []
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
            ys, xs = np.mgrid[y0:y1:step, x0:x1:step]
            if ys.size == 0:
                continue
            xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
            delta = np.asarray(predict(xy), dtype=float) - field.sample(xy)
            errors.append(np.hypot(delta[:, 0], delta[:, 1]))

    if not errors:
        return {"mean_px": float("nan"), "median_px": float("nan"),
                "p95_px": float("nan"), "max_px": float("nan"), "n": 0}

    allerr = np.concatenate(errors)
    return {
        "mean_px": float(allerr.mean()),
        "median_px": float(np.median(allerr)),
        "p95_px": float(np.percentile(allerr, 95)),
        "max_px": float(allerr.max()),
        "n": int(allerr.size),
    }
