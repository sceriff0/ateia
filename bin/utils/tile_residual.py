"""Per-tile refinement primitive for the STARE registration method.

After the global M0 anchor, a tile's moving crop and the reference crop differ only by a small,
near-translational residual. :func:`residual_displacement` recovers it sub-pixel with phase
correlation and returns it as ``(dx, dy)`` — the mesh control-point displacement for the tile,
i.e. the amount to add to a moving-tile coordinate to land it on the reference — together with the
tile's local Target Registration Error (the magnitude of that residual) and the correlation's
normalised error, which is how a caller tells a real match from a peak in noise.

Pure NumPy + scikit-image.
"""

from __future__ import annotations

import numpy as np

__all__ = ["residual_displacement"]


def residual_displacement(ref_tile, mov_tile, upsample=10, whiten_sigma=3.0):
    """Sub-pixel residual displacement aligning ``mov_tile`` to ``ref_tile``.

    Returns ``(dx, dy, tre, error)``:

    - ``(dx, dy)`` is the control-point displacement (add it to a moving coordinate to reach the
      reference);
    - ``tre = hypot(dx, dy)`` is the tile's local rigid-stage TRE — the pre-refinement
      misalignment magnitude used to gate whether the tile needs refining;
    - ``error`` is scikit-image's normalised correlation error, in ``[0, 1]``, where 0 is a
      perfect match and ~1 means the two crops share no structure at all.

    ``error`` is the *confidence* of the match and is not optional bookkeeping. Phase correlation
    always returns a peak, so a background tile or a tile straddling the section edge yields a
    displacement that is indistinguishable from a small real residual by magnitude — measured on
    the synthetic tiles in ``tests/test_tile_residual_confidence.py``, a tissue-vs-background tile
    scores ``|d| = 5.66 px`` (well inside ``reg_tiled_halo``) and only its ``error`` of 0.999961,
    against real tissue's 0.040956, gives it away. ``tiled_solve._grid_from_controls`` rejects
    control points above ``--max-error`` for exactly that reason.

    ``error`` is **NaN** where scikit-image cannot compute it at all — notably when either crop is
    empty, which is what ``tiled_reg_tile.py`` manufactures when the moving crop falls outside the
    slide. Callers must treat NaN as "reject", not as "small".

    The correlation kernel mirrors ASHLAR's: **whiten** each crop with a high-pass (subtract a
    Gaussian blur) to strip the low-frequency intensity mismatch between stains, then **Hann
    window** to taper the non-periodic tile edges the FFT would otherwise wrap. Plain (un-phase-
    normalised) cross-correlation is used deliberately — phase normalisation amplifies the
    high-frequency noise the whitening leaves and destabilises the sub-pixel peak on small tiles.
    """
    from scipy.ndimage import gaussian_filter
    from skimage.filters import window
    from skimage.registration import phase_cross_correlation

    ref = np.asarray(ref_tile, dtype=float)
    mov = np.asarray(mov_tile, dtype=float)
    win = window("hann", ref.shape)
    ref_p = (ref - gaussian_filter(ref, whiten_sigma)) * win
    mov_p = (mov - gaussian_filter(mov, whiten_sigma)) * win
    # shift registers `mov` onto `ref`; axis order is (row, col) = (y, x)
    shift, error, _phasediff = phase_cross_correlation(
        ref_p, mov_p, upsample_factor=upsample, normalization=None
    )
    dy = float(shift[0])
    dx = float(shift[1])
    tre = float(np.hypot(dx, dy))
    return dx, dy, tre, float(error)
