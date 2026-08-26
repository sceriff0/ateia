"""Is the recovered warp a valid deformation, and is it invertible?

TWO NUMBERS THE PIPELINE CURRENTLY ASSUMES.

Folding rate: the fraction of the field where the Jacobian determinant goes
negative, i.e. where the warp turns tissue inside out. A method can score a
good endpoint error while folding, and folded output silently corrupts
downstream per-cell quantification.

Lipschitz constant: bin/utils/tiled_warp.py:_invert inverts the warp by three
fixed-point iterations of x <- M0^-1(u - F(x)). That iteration converges only
when the displacement field's Lipschitz constant is below 1. Nothing in the
pipeline verifies it. Measuring it turns an unchecked precondition into a
reported number.

EXACT vs ESTIMATED in ``jacobian_stats``, and why both exist in the same dict:

``folding_rate``, ``det_min`` and ``n`` are EXACT over every point on the
raster. They are accumulated as a running count, a running minimum and a
running total -- one scalar each, never a per-point array -- so they cost O(1)
memory regardless of raster size.

``det_median`` and ``det_p05`` are ESTIMATES from a deterministically retained
subset of at most ``max_points`` determinant values, for the same reason
``epe.py`` caps its percentile accumulator: a percentile needs the sorted
distribution, not just a scalar reduction, so *some* array has to be held, and
at the gigapixel rung holding all of them would reconstruct the very
whole-raster array this module exists to avoid materialising. The retention is
a deterministic stride over a global point index (never reservoir sampling,
never anything seeded). ``subsample_effective`` (the stride actually used) and
``n_percentile`` (how many points fed the percentiles) are returned alongside
the estimates so a reader can see which numbers in the row are exact and
reproduce the retained subset exactly.

``lipschitz`` has no percentile to estimate -- it already tracks a single
running maximum, an O(1) accumulator -- so it needs no such split.

Both functions call ``predict`` only one tile's worth of points at a time, so
*that* per-call footprint is bounded by ``tile``; peak memory for the returned
statistics is bounded separately as described above, by ``max_points`` in
``jacobian_stats`` and trivially (a single running max) in ``lipschitz``.
"""

from __future__ import annotations

import numpy as np

__all__ = ["jacobian_stats", "lipschitz"]


def _grads(predict, xs, ys, step):
    """Central-difference d(displacement)/d(x, y) at the given grid points."""
    xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
    off = np.array([step, 0.0])
    dx = (np.asarray(predict(xy + off)) - np.asarray(predict(xy - off))) / (2 * step)
    off = np.array([0.0, step])
    dy = (np.asarray(predict(xy + off)) - np.asarray(predict(xy - off))) / (2 * step)
    return dx, dy


def jacobian_stats(predict, shape, tile=512, step=1.0, max_points=50_000_000):
    """Determinant statistics of the warp Jacobian ``I + grad(displacement)``.

    ``folding_rate``/``det_min``/``n`` are exact over every point on the
    raster. ``det_median``/``det_p05`` are estimated from a deterministically
    retained subsample of at most ``max_points`` determinant values;
    ``subsample_effective`` and ``n_percentile`` describe exactly which points
    those were.
    """
    h, w = int(shape[0]), int(shape[1])

    # Pass 1: pure grid arithmetic, no predict calls -- so the total point
    # count is known before the percentile retention stride is chosen,
    # without ever materialising a determinant array.
    blocks = []
    n_total = 0
    for y0 in range(0, h, tile):
        y1 = min(y0 + tile, h)
        for x0 in range(0, w, tile):
            x1 = min(x0 + tile, w)
            ny, nx = y1 - y0, x1 - x0
            if ny <= 0 or nx <= 0:
                continue
            blocks.append((y0, y1, x0, x1))
            n_total += ny * nx

    if n_total == 0:
        return {"folding_rate": float("nan"), "det_min": float("nan"),
                "det_p05": float("nan"), "det_median": float("nan"), "n": 0,
                "subsample_effective": 1, "n_percentile": 0}

    max_points = max(1, int(max_points))
    # Ceiling division: the smallest stride that keeps the retained count at
    # or under max_points, however large n_total is.
    keep_every = -(-n_total // max_points) if n_total > max_points else 1

    # Pass 2: the real (and only) sweep over predict. Exact accumulators are
    # scalars; the percentile accumulator only ever holds points selected by
    # the keep_every stride on the global point index.
    neg_count = 0
    det_min = float("inf")
    seen = 0
    kept = []
    global_idx = 0
    for y0, y1, x0, x1 in blocks:
        ys, xs = np.mgrid[y0:y1, x0:x1]
        dx, dy = _grads(predict, xs, ys, step)
        a = 1.0 + dx[:, 0]   # d(x + ux)/dx
        b = dy[:, 0]         # d(x + ux)/dy
        c = dx[:, 1]         # d(y + uy)/dx
        d = 1.0 + dy[:, 1]   # d(y + uy)/dy
        det = a * d - b * c

        neg_count += int((det <= 0).sum())
        if det.size:
            block_min = float(det.min())
            if block_min < det_min:
                det_min = block_min
        seen += det.size

        idx = np.arange(global_idx, global_idx + det.size)
        mask = (idx % keep_every) == 0
        if mask.any():
            kept.append(det[mask])
        global_idx += det.size

    kept_arr = np.concatenate(kept) if kept else np.empty(0, dtype=float)

    return {
        "folding_rate": float(neg_count / seen),
        "det_min": float(det_min),
        "det_p05": float(np.percentile(kept_arr, 5)),
        "det_median": float(np.median(kept_arr)),
        "n": int(seen),
        "subsample_effective": int(keep_every),
        "n_percentile": int(kept_arr.size),
    }


def lipschitz(predict, shape, tile=512, step=1.0):
    """Sup-norm Lipschitz constant of the DISPLACEMENT field.

    Estimated as the maximum spectral norm of grad(displacement) over the
    raster, using the 2x2 spectral norm in closed form. Already a true O(1)
    streaming reduction -- ``best``/``n`` are a running maximum and a running
    count, never a per-point array -- so, unlike ``jacobian_stats``, this has
    no percentile to estimate and needs no ``max_points`` split.
    """
    h, w = int(shape[0]), int(shape[1])
    best = 0.0
    n = 0
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            ys, xs = np.mgrid[y0:min(y0 + tile, h), x0:min(x0 + tile, w)]
            if ys.size == 0:
                continue
            dx, dy = _grads(predict, xs, ys, step)
            a, c = dx[:, 0], dx[:, 1]
            b, d = dy[:, 0], dy[:, 1]
            # Largest singular value of [[a, b], [c, d]], closed form.
            s1 = a * a + b * b + c * c + d * d
            s2 = np.sqrt(np.maximum((a * a + b * b - c * c - d * d) ** 2
                                    + 4 * (a * c + b * d) ** 2, 0.0))
            sigma = np.sqrt(np.maximum((s1 + s2) / 2.0, 0.0))
            best = max(best, float(sigma.max()))
            n += int(sigma.size)

    if n == 0:
        # A zero-size raster measured nothing -- "converges: True" would be a
        # convergence claim from no data. Mirror jacobian_stats' own empty
        # branch (NaN, not a fabricated default) rather than a bare True.
        return {"lipschitz": float("nan"), "converges": float("nan"), "n": 0}

    return {"lipschitz": best, "converges": bool(best < 1.0), "n": n}
