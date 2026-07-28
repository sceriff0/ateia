"""Image warp for the STARE registration method (the WARP_TILE core).

Warps a moving image into the reference frame through the *same* transform the reg_qc=2 stage
warper uses — global affine ``M0`` plus the smooth mesh residual ``F`` — so the QC measures
exactly what ships. Warping resamples by the **inverse** map: for each reference-frame output
pixel ``u`` it finds the source moving coordinate ``x`` solving the forward relation
``u = M0·x + F(x)``, then samples the image there with :func:`mesh_field.resample_bilinear`. Because
``F`` is small and smooth, the inverse is found by a few fixed-point iterations
``x <- M0^-1 (u - F(x))``; with no mesh it is the exact affine inverse.

Bilinear resampling keeps a non-negative image non-negative — the guarantee downstream marker
quantification relies on. Pure NumPy.
"""

from __future__ import annotations

import numpy as np
from mesh_field import resample_bilinear

__all__ = ["warp_image"]


def _apply_affine(m, xy):
    homog = np.column_stack([xy, np.ones(len(xy))])
    return (homog @ np.asarray(m, dtype=float).T)[:, :2]


def warp_image(image, m0, mesh, out_shape, mesh_inverse_iters=3):
    """Warp ``image`` (moving) into an ``out_shape`` = ``(H, W)`` reference-frame raster.

    Parameters
    ----------
    image : ndarray, ``(H, W)`` or ``(H, W, C)``
        The moving image (non-negative).
    m0 : 3x3 array
        Global forward affine, moving -> reference.
    mesh : MeshField or None
        Residual displacement field (sampled at moving coordinates), or None for a rigid warp.
    out_shape : (int, int)
        Output raster size ``(height, width)`` in the reference frame.
    """
    image = np.asarray(image, dtype=float)
    m0 = np.asarray(m0, dtype=float)
    out_h, out_w = out_shape

    ys, xs = np.mgrid[0:out_h, 0:out_w]
    u = np.column_stack([xs.ravel().astype(float), ys.ravel().astype(float)])

    m_inv = np.linalg.inv(m0)
    x = _apply_affine(m_inv, u)
    if mesh is not None:
        # solve u = M0 x + F(x) for x by fixed-point; F is small so this converges in a few steps
        for _ in range(mesh_inverse_iters):
            x = _apply_affine(m_inv, u - mesh.displacement(x))

    vals = resample_bilinear(image, x)
    if image.ndim == 3:
        return vals.reshape(out_h, out_w, image.shape[2])
    return vals.reshape(out_h, out_w)
