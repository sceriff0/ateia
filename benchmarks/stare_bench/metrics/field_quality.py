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

Both reduce tile by tile, so peak memory is bounded by ``tile``.
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


def jacobian_stats(predict, shape, tile=512, step=1.0):
    """Determinant statistics of the warp Jacobian ``I + grad(displacement)``."""
    h, w = int(shape[0]), int(shape[1])
    dets = []
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            ys, xs = np.mgrid[y0:min(y0 + tile, h), x0:min(x0 + tile, w)]
            if ys.size == 0:
                continue
            dx, dy = _grads(predict, xs, ys, step)
            a = 1.0 + dx[:, 0]   # d(x + ux)/dx
            b = dy[:, 0]         # d(x + ux)/dy
            c = dx[:, 1]         # d(y + uy)/dx
            d = 1.0 + dy[:, 1]   # d(y + uy)/dy
            dets.append(a * d - b * c)

    if not dets:
        return {"folding_rate": float("nan"), "det_min": float("nan"),
                "det_p05": float("nan"), "det_median": float("nan"), "n": 0}
    det = np.concatenate(dets)
    return {
        "folding_rate": float((det <= 0).mean()),
        "det_min": float(det.min()),
        "det_p05": float(np.percentile(det, 5)),
        "det_median": float(np.median(det)),
        "n": int(det.size),
    }


def lipschitz(predict, shape, tile=512, step=1.0):
    """Sup-norm Lipschitz constant of the DISPLACEMENT field.

    Estimated as the maximum spectral norm of grad(displacement) over the
    raster, using the 2x2 spectral norm in closed form.
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

    return {"lipschitz": best, "converges": bool(best < 1.0), "n": n}
