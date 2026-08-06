"""Shared utilities for image registration.

This module holds registration helpers shared by other ``bin/`` scripts
(currently ``utils/qc.py``) to eliminate code duplication.

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

import numpy as np
from numpy.typing import NDArray

__all__ = ["autoscale"]


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
