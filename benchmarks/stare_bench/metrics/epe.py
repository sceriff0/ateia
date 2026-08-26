"""Endpoint error against the known field -- the metric landmarks cannot give.

Ground truth exists at EVERY pixel here, not at a few hundred hand-placed
points, which is the whole reason synthetic data beats ANHIR/ACROBAT for this
problem. Manual landmarks carry a ~115 um annotation floor; this has none.

EXACT vs ESTIMATED, and why both exist in the same dict:

``mean_px``, ``max_px`` and ``n`` are EXACT over every sampled point on the
raster (after ``subsample``, if given). They are accumulated as a running sum,
a running max and a running count -- one scalar each, never a per-point array
-- so they cost O(1) memory regardless of raster size.

``median_px`` and ``p95_px`` are ESTIMATES from a deterministically retained
subset of at most ``max_points`` errors. A percentile needs the sorted
distribution, not just a scalar reduction, so *some* array has to be held; at
the gigapixel rung (~2.6e9 sampled points) holding all of them would be ~21 GB
-- the same order of magnitude as the full 2-component field this module
exists to avoid materialising. The retention is a deterministic stride over a
global point index (never reservoir sampling, never anything seeded): a
benchmark's own measurement must not carry a hidden RNG. ``subsample_effective``
(the combined stride: ``subsample`` composed with the stride the cap forced)
and ``n_percentile`` (how many points actually fed the percentiles) are
returned alongside the estimates so a reader can see which numbers in the row
are exact and reproduce the retained subset exactly.

REDUCTION IS STREAMING, tile by tile: ``predict`` is only ever called with one
tile's worth of points, so *tile* is what the batches passed to ``predict``
are bounded by. The percentile accumulator is bounded separately, by
``max_points`` -- these are independent knobs and both must hold, not just
the first.
"""

from __future__ import annotations

import numpy as np

__all__ = ["epe"]


def epe(field, predict, shape, tile=1024, subsample=1, max_points=50_000_000):
    """Endpoint error of ``predict`` against ``field`` over a ``shape`` raster.

    ``mean_px``/``max_px``/``n`` are exact over every sampled point.
    ``median_px``/``p95_px`` are estimated from a deterministically retained
    subsample of at most ``max_points`` points; ``subsample_effective`` and
    ``n_percentile`` describe exactly which points those were.
    """
    h, w = int(shape[0]), int(shape[1])
    step = max(1, int(subsample))

    # Pass 1: pure grid arithmetic, no field/predict calls -- so the total
    # sampled-point count is known before the percentile retention stride is
    # chosen, without ever materialising a point array.
    blocks = []
    n_total = 0
    for y0 in range(0, h, tile):
        y1 = min(y0 + tile, h)
        ny = len(range(y0, y1, step))
        if ny == 0:
            continue
        for x0 in range(0, w, tile):
            x1 = min(x0 + tile, w)
            nx = len(range(x0, x1, step))
            if nx == 0:
                continue
            blocks.append((y0, y1, x0, x1))
            n_total += ny * nx

    if n_total == 0:
        return {"mean_px": float("nan"), "median_px": float("nan"),
                "p95_px": float("nan"), "max_px": float("nan"), "n": 0,
                "subsample_effective": step, "n_percentile": 0}

    max_points = max(1, int(max_points))
    # Ceiling division: the smallest stride that keeps the retained count at
    # or under max_points, however large n_total is.
    keep_every = -(-n_total // max_points) if n_total > max_points else 1

    # Pass 2: the real (and only) sweep over predict/field.sample. Exact
    # accumulators are scalars; the percentile accumulator only ever holds
    # points selected by the keep_every stride on the global point index.
    total_sum = 0.0
    total_max = 0.0
    seen = 0
    kept = []
    global_idx = 0
    for y0, y1, x0, x1 in blocks:
        ys, xs = np.mgrid[y0:y1:step, x0:x1:step]
        xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
        delta = np.asarray(predict(xy), dtype=float) - field.sample(xy)
        err = np.hypot(delta[:, 0], delta[:, 1])

        total_sum += float(err.sum())
        if err.size:
            block_max = float(err.max())
            if block_max > total_max:
                total_max = block_max
        seen += err.size

        idx = np.arange(global_idx, global_idx + err.size)
        mask = (idx % keep_every) == 0
        if mask.any():
            kept.append(err[mask])
        global_idx += err.size

    kept_arr = np.concatenate(kept) if kept else np.empty(0, dtype=float)

    return {
        "mean_px": float(total_sum / seen),
        "median_px": float(np.median(kept_arr)),
        "p95_px": float(np.percentile(kept_arr, 95)),
        "max_px": float(total_max),
        "n": int(seen),
        "subsample_effective": int(step * keep_every),
        "n_percentile": int(kept_arr.size),
    }
