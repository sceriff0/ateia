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

import numpy as np

__all__ = [
    "estimate_transform_from_matches",
    "estimate_rigid",
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


def _orb_features(img, n_keypoints, fast_threshold):
    from skimage.feature import ORB

    orb = ORB(n_keypoints=n_keypoints, fast_threshold=fast_threshold)
    orb.detect_and_extract(np.asarray(img, dtype=float))
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
    """
    from skimage.feature import match_descriptors

    kp_ref, d_ref = _orb_features(ref, n_keypoints, fast_threshold)
    kp_mov, d_mov = _orb_features(mov, n_keypoints, fast_threshold)
    matches = match_descriptors(d_mov, d_ref, cross_check=True)
    src = kp_mov[matches[:, 0]]
    dst = kp_ref[matches[:, 1]]
    return estimate_transform_from_matches(src, dst, model=model, **kwargs)
