"""Shared utilities for image registration.

This module consolidates common functions used across registration scripts
(register.py) to eliminate code duplication.

Examples
--------
>>> from registration_utils import autoscale
>>> scaled = autoscale(image, low_p=1.0, high_p=99.0)

Notes
-----
This module implements the DRY principle by providing single implementations
of functions that were previously duplicated across multiple scripts.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "autoscale",
    "build_feature_detector",
    "build_feature_matcher",
    "match_feature_points",
]


def autoscale(
    img: NDArray, low_p: float = 1.0, high_p: float = 99.0
) -> NDArray[np.uint8]:
    """Normalize image to 0-255 using percentile-based scaling.

    This function implements the same auto-scaling behavior as ImageJ's
    "Auto" brightness adjustment, using percentile clipping to handle
    outliers robustly.

    Parameters
    ----------
    img : NDArray
        Input image array of any numeric dtype.
    low_p : float, default=1.0
        Lower percentile for clipping (0-100).
        Values below this percentile are mapped to 0.
    high_p : float, default=99.0
        Upper percentile for clipping (0-100).
        Values above this percentile are mapped to 255.

    Returns
    -------
    NDArray[np.uint8]
        Scaled image in uint8 format (0-255 range).

    Notes
    -----
    The scaling process:
    1. Compute percentile values at `low_p` and `high_p`
    2. Clip image values to [low, high] range
    3. Linearly scale to [0, 1]
    4. Multiply by 255 and convert to uint8

    Using percentiles instead of min/max makes the scaling robust to
    outliers and saturated pixels.

    Examples
    --------
    >>> img = np.random.randint(0, 4096, size=(100, 100), dtype=np.uint16)
    >>> scaled = autoscale(img)
    >>> scaled.dtype
    dtype('uint8')
    >>> scaled.min(), scaled.max()
    (0, 255)

    Conservative scaling (wider range):
    >>> scaled = autoscale(img, low_p=0.1, high_p=99.9)

    See Also
    --------
    numpy.percentile : Compute percentiles
    numpy.clip : Clip values to range
    """
    lo = np.percentile(img, low_p)
    hi = np.percentile(img, high_p)

    # Avoid division by zero
    range_val = max(hi - lo, 1e-6)

    # Clip and normalize to [0, 1]
    img_normalized = np.clip((img - lo) / range_val, 0, 1)

    # Scale to [0, 255] and convert to uint8
    # Fix: Round before converting to uint8 to avoid truncation artifacts
    return np.round(img_normalized * 255).astype(np.uint8)


def build_feature_detector(
    detector_type: str = "superpoint", logger: Optional[Any] = None
):
    """Create a VALIS feature detector instance for the selected detector."""
    from valis import feature_detectors

    detector = detector_type.lower()
    detector_map = {
        "superpoint": feature_detectors.SuperPointFD,
        "brisk": feature_detectors.BriskFD,
        "vgg": feature_detectors.VggFD,
    }
    if detector not in detector_map:
        raise ValueError(f"Unknown detector type: {detector_type}")

    if logger:
        logger.info(f"Initializing feature detector: {detector}")
    return detector_map[detector]()


def build_feature_matcher(
    detector_type: str = "superpoint", logger: Optional[Any] = None
):
    """Create a matcher compatible with the selected detector."""
    from valis import feature_matcher

    detector = detector_type.lower()
    if detector == "superpoint":
        if logger:
            logger.info("Initializing SuperGlue matcher")
        return feature_matcher.SuperGlueMatcher()
    if detector in {"disk", "dedode"}:
        if logger:
            logger.info("Initializing LightGlue matcher")
        return feature_matcher.LightGlueMatcher()
    if logger:
        logger.info("Initializing Matcher with USAC_MAGSAC filter")
    return feature_matcher.Matcher(match_filter_method="USAC_MAGSAC", ransac_thresh=7)


def match_feature_points(
    matcher: Any,
    ref_img: NDArray,
    ref_desc: NDArray,
    ref_kp: NDArray,
    mov_img: NDArray,
    mov_desc: NDArray,
    mov_kp: NDArray,
) -> Tuple[Any, Any, Any, Any]:
    """Run feature matching with the correct matcher-specific signature."""
    from valis import feature_matcher

    if isinstance(matcher, feature_matcher.SuperGlueMatcher) or (
        hasattr(feature_matcher, "LightGlueMatcher")
        and isinstance(matcher, feature_matcher.LightGlueMatcher)
    ):
        return matcher.match_images(
            img1=ref_img,
            desc1=ref_desc,
            kp1_xy=ref_kp,
            img2=mov_img,
            desc2=mov_desc,
            kp2_xy=mov_kp,
        )

    return matcher.match_images(
        desc1=ref_desc,
        kp1_xy=ref_kp,
        desc2=mov_desc,
        kp2_xy=mov_kp,
    )
