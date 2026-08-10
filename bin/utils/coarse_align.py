"""Global rigid anchor (M0) estimation for the STARE registration method.

The COARSE step aligns a whole moving slide to the reference with a single affine ``M0``, cheaply,
on downsampled images. Feature matching (ORB) handles the inter-cycle rotation/translation that a
translation-only phase correlation could not; the per-tile step later refines the small residual.

The numerically load-bearing piece is :func:`estimate_transform_from_matches` — given point
correspondences it solves for the transform and reports a residual Target Registration Error. It
is deterministic and unit-tested. :func:`estimate_rigid` wraps it with the ORB feature front-end.

Pure NumPy + scikit-image (no VALIS, no JVM).
"""

from __future__ import annotations

import time

import numpy as np
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "estimate_transform_from_matches",
    "estimate_rigid",
    "normalize_for_orb",
    "scale_transform_to_full_res",
]

_MIN_SAMPLES = {"euclidean": 2, "similarity": 2, "affine": 3}


def _model_class(model):
    from skimage.transform import (
        AffineTransform,
        EuclideanTransform,
        SimilarityTransform,
    )

    return {
        "euclidean": EuclideanTransform,
        "similarity": SimilarityTransform,
        "affine": AffineTransform,
    }[model]


def _ransac(data, cls, min_samples, residual_threshold, max_trials, random_state):
    """Call skimage.measure.ransac across the rng/random_state kwarg rename."""
    from skimage.measure import ransac

    kw = dict(
        min_samples=min_samples,
        residual_threshold=residual_threshold,
        max_trials=max_trials,
    )
    try:
        return ransac(data, cls, rng=random_state, **kw)
    except TypeError:
        return ransac(data, cls, random_state=random_state, **kw)


def _rms(residuals_xy):
    if len(residuals_xy) == 0:
        return float("nan")
    return float(np.sqrt(np.mean(np.sum(np.asarray(residuals_xy) ** 2, axis=1))))


def estimate_transform_from_matches(
    src,
    dst,
    model="euclidean",
    robust=True,
    residual_threshold=3.0,
    max_trials=1000,
    random_state=0,
):
    """Estimate the transform mapping ``src`` -> ``dst`` and its residual TRE.

    Returns ``(M, residual_px, n_inliers)`` where ``M`` is the 3x3 forward affine, ``residual_px``
    is the RMS distance between the transformed inlier ``src`` and ``dst`` (the fit's Target
    Registration Error), and ``n_inliers`` is how many correspondences the fit used.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if model not in _MIN_SAMPLES:
        raise ValueError(
            f"unknown model {model!r}; expected one of {list(_MIN_SAMPLES)}"
        )

    cls = _model_class(model)
    if robust and len(src) > _MIN_SAMPLES[model]:
        tform, inliers = _ransac(
            (src, dst),
            cls,
            _MIN_SAMPLES[model],
            residual_threshold,
            max_trials,
            random_state,
        )
        if tform is None:  # ransac failed to find a consensus set
            inliers = np.ones(len(src), dtype=bool)
            tform = cls()
            tform.estimate(src, dst)
    else:
        inliers = np.ones(len(src), dtype=bool)
        tform = cls()
        tform.estimate(src, dst)

    m = np.asarray(tform.params, dtype=float)
    residual = _rms(tform(src[inliers]) - dst[inliers])
    return m, residual, int(np.count_nonzero(inliers))


def scale_transform_to_full_res(m, factor):
    """Lift a transform estimated on ``factor``-decimated images back to full-resolution pixels.

    A decimated coordinate relates to the full-resolution one by ``p_ds = p_full / factor``, so
    the full-resolution map is ``diag(f, f, 1) @ M_ds @ diag(1/f, 1/f, 1)``. The two scalings
    cancel across the linear 2x2 block and survive only on the translation column -- i.e. the
    rotation/scale part is invariant and only the offsets grow by ``factor``. Returning ``M_ds``
    unchanged is the natural mistake, and it under-translates by exactly ``factor``.
    """
    m = np.array(m, dtype=float, copy=True)
    factor = float(factor)
    m[:2, 2] *= factor
    return m


# ORB's `fast_threshold` is an ABSOLUTE intensity difference, and skimage's `img_as_float`
# rescales only INTEGER inputs -- a float array passes through with its scale untouched. So the
# threshold is only meaningful on data already in [0, 1]. The planes reaching this module are raw
# microscopy counts (uint16, 0..65535) widened to float32 by `tiled_io.read_decimated`, where 0.05
# sits ~6 orders of magnitude below the data range: FAST then fires on ~38% of ALL pixels instead
# of ~3%, and `corner_peaks`' spacing pass -- quadratic in the candidate count -- turns a ~1 min
# anchor into a ~5 h one, past TILED_COARSE's 2 h walltime. Measured on a 2048^2 thumbnail:
# 1.1 s normalised vs 319 s raw; the gap widens with area (~x15.7 per x4 px).
#
# Normalising is also a CORRECTNESS fix, not only a speed one: reference and moving come from
# different imaging cycles with different exposures, so one absolute threshold applied to two
# different dynamic ranges can yield hundreds of keypoints on one slide and almost none on the
# other -- a starved or bogus M0, with no error raised.
#
# A percentile window, not `img / img.max()`: one hot pixel (routine in fluorescence) would
# otherwise compress the whole tissue into the bottom of the range and starve ORB of corners.
# p99.9 rather than p99 because DAPI nuclei ARE the bright minority -- clipping at p99 saturates
# the very structure the anchor matches on.
_ORB_PCT = (1.0, 99.9)


def normalize_for_orb(img):
    """Rescale ``img`` to [0, 1] over a robust percentile window (see the note above)."""
    a = np.asarray(img, dtype=float)
    lo, hi = (float(v) for v in np.percentile(a, _ORB_PCT))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return np.zeros_like(a)  # constant/all-NaN plane: no corners to find
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _orb_features(img, n_keypoints, fast_threshold):
    from skimage.feature import ORB

    orb = ORB(n_keypoints=n_keypoints, fast_threshold=fast_threshold)
    orb.detect_and_extract(normalize_for_orb(img))
    # ORB keypoints are (row, col); the rest of the pipeline is (x, y)
    return orb.keypoints[:, ::-1], orb.descriptors


def estimate_rigid(
    ref,
    mov,
    model="euclidean",
    n_keypoints=800,
    fast_threshold=0.05,
    **kwargs,
):
    """Estimate ``M0`` mapping ``mov`` image coordinates onto ``ref`` via ORB feature matching.

    Returns ``(M0, residual_px, n_inliers)`` from :func:`estimate_transform_from_matches`.

    Logs each stage as it completes. ORB dominates this step's runtime and its cost is highly
    non-linear in thumbnail size, so a silent multi-hour stage here is indistinguishable from a
    hang -- which is exactly how the un-normalised-input regression above presented.
    """
    from skimage.feature import match_descriptors

    t0 = time.perf_counter()
    kp_ref, d_ref = _orb_features(ref, n_keypoints, fast_threshold)
    t_ref = time.perf_counter() - t0
    logger.info(
        f"coarse: ORB reference -> {len(kp_ref)} keypoints in {t_ref:.1f}s "
        f"({ref.shape[1]}x{ref.shape[0]})"
    )

    t1 = time.perf_counter()
    kp_mov, d_mov = _orb_features(mov, n_keypoints, fast_threshold)
    t_mov = time.perf_counter() - t1
    logger.info(
        f"coarse: ORB moving    -> {len(kp_mov)} keypoints in {t_mov:.1f}s "
        f"({mov.shape[1]}x{mov.shape[0]})"
    )

    matches = match_descriptors(d_mov, d_ref, cross_check=True)
    logger.info(
        f"coarse: {len(matches)} cross-checked matches -> {model} fit "
        f"(ORB total {t_ref + t_mov:.1f}s)"
    )
    if len(matches) < _MIN_SAMPLES[model]:
        # estimate_transform_from_matches would fall through to an unconstrained .estimate()
        # and hand back a silently meaningless M0; say so while the slide names are still in
        # the surrounding log context.
        logger.warning(
            f"coarse: only {len(matches)} matches for a {model} fit "
            f"(needs >= {_MIN_SAMPLES[model]}) -- M0 will be unreliable"
        )

    src = kp_mov[matches[:, 0]]
    dst = kp_ref[matches[:, 1]]
    return estimate_transform_from_matches(src, dst, model=model, **kwargs)
