"""Image value validation and negative value handling utilities.

This module provides functions for detecting, logging, and handling negative
pixel values that can be introduced during image processing (e.g., BaSiC
illumination correction, bicubic interpolation).

Examples
--------
>>> from utils.validation import log_image_stats, clip_negative_values
>>> data = load_image()
>>> log_image_stats(data, "after_basic", logger)
>>> data = clip_negative_values(data, logger)
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "log_image_stats",
    "detect_negative_values",
    "clip_negative_values",
    "detect_wrapped_values",
    "StreamingNegativeClip",
    "StreamingImageStats",
]


def log_image_stats(
    data: NDArray, stage_name: str, logger: Optional[logging.Logger] = None
) -> None:
    """Log image statistics for debugging and validation.

    Parameters
    ----------
    data : NDArray
        Image data array.
    stage_name : str
        Name of the processing stage (e.g., "input", "after_basic", "registered").
    logger : logging.Logger, optional
        Logger to use. If None, creates one for this module.

    Returns
    -------
    None
    """
    _logger = logger or logging.getLogger(__name__)

    min_val = data.min()
    max_val = data.max()
    mean_val = data.mean()

    _logger.info(
        f"[{stage_name}] Image stats: dtype={data.dtype}, shape={data.shape}, "
        f"min={min_val}, max={max_val}, mean={mean_val:.2f}"
    )

    # Warn about suspicious values
    if min_val < 0:
        _logger.warning(f"[{stage_name}] Negative values detected: min={min_val}")

    if data.dtype == np.uint16 and max_val > 60000:
        high_count = np.sum(data > 60000)
        high_pct = high_count / data.size * 100
        if high_pct > 0.1:
            _logger.warning(
                f"[{stage_name}] Suspicious high values (potential wrapped negatives): "
                f"{high_count} pixels ({high_pct:.2f}%) > 60000"
            )


def detect_negative_values(
    data: NDArray, logger: Optional[logging.Logger] = None
) -> Tuple[bool, int, float]:
    """Detect negative values in image data.

    For signed types (int16, float32), checks for actual negative values.
    For unsigned types (uint8, uint16), negative values cannot exist but
    may have wrapped around - use detect_wrapped_values() for those.

    Parameters
    ----------
    data : NDArray
        Image data array.
    logger : logging.Logger, optional
        Logger to use for warnings.

    Returns
    -------
    has_negatives : bool
        True if negative values were found.
    count : int
        Number of negative pixels.
    percentage : float
        Percentage of pixels that are negative.
    """
    _logger = logger or logging.getLogger(__name__)

    if not np.issubdtype(data.dtype, np.signedinteger) and not np.issubdtype(
        data.dtype, np.floating
    ):
        # Unsigned type - cannot have actual negatives
        return False, 0, 0.0

    neg_mask = data < 0
    count = int(np.sum(neg_mask))
    percentage = count / data.size * 100

    has_negatives = count > 0

    if has_negatives:
        min_val = data[neg_mask].min()
        _logger.warning(
            f"Detected {count} negative pixels ({percentage:.4f}%), "
            f"min value: {min_val}"
        )

    return has_negatives, count, percentage


def detect_wrapped_values(
    data: NDArray,
    threshold_percentile: float = 99.5,
    min_value_threshold: float = 0.9,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, int, float]:
    """Detect potentially wrapped negative values in unsigned integer data.

    When negative float values are cast to uint16, they wrap around:
    -1 -> 65535, -100 -> 65435, etc. This function detects such values
    by looking for suspiciously high values.

    Parameters
    ----------
    data : NDArray
        Image data array.
    threshold_percentile : float, default=99.5
        Percentile threshold for detecting outliers.
    min_value_threshold : float, default=0.9
        Fraction of dtype max above which values are suspicious.
    logger : logging.Logger, optional
        Logger to use for warnings.

    Returns
    -------
    has_wrapped : bool
        True if wrapped values were likely detected.
    count : int
        Number of suspicious pixels.
    percentage : float
        Percentage of suspicious pixels.
    """
    _logger = logger or logging.getLogger(__name__)

    if data.dtype == np.uint16:
        dtype_max = 65535
    elif data.dtype == np.uint8:
        dtype_max = 255
    else:
        # Not an unsigned integer type
        return False, 0, 0.0

    # Calculate threshold: values above this are suspicious
    absolute_threshold = dtype_max * min_value_threshold
    # Flag values above absolute threshold as suspicious
    suspicious_mask = data > absolute_threshold
    count = int(np.sum(suspicious_mask))
    percentage = count / data.size * 100

    has_wrapped = count > 0 and percentage > 0.01  # More than 0.01% suspicious

    if has_wrapped:
        max_val = data[suspicious_mask].max() if count > 0 else 0
        _logger.warning(
            f"Detected {count} potentially wrapped pixels ({percentage:.4f}%), "
            f"max value: {max_val}"
        )

    return has_wrapped, count, percentage


def clip_negative_values(
    data: NDArray, logger: Optional[logging.Logger] = None, stage_name: str = "unknown"
) -> NDArray:
    """Clip negative values to zero with logging.

    For float or signed integer data, clips values below 0 to 0.
    For unsigned data, no clipping is needed (values can't be negative).

    Parameters
    ----------
    data : NDArray
        Image data array. Modified in place if possible.
    logger : logging.Logger, optional
        Logger to use.
    stage_name : str, default="unknown"
        Name of processing stage for logging.

    Returns
    -------
    NDArray
        Clipped image data (may be same array if modified in place).
    """
    _logger = logger or logging.getLogger(__name__)

    # Check if data can have negative values
    if np.issubdtype(data.dtype, np.unsignedinteger):
        _logger.debug(f"[{stage_name}] Unsigned dtype, no negative clipping needed")
        return data

    # Detect negatives before clipping
    has_neg, count, pct = detect_negative_values(data, _logger)

    if has_neg:
        min_before = data.min()
        data = np.clip(data, 0, None)
        _logger.info(
            f"[{stage_name}] Clipped {count} negative pixels ({pct:.4f}%) to 0. "
            f"Min before: {min_before}, after: {data.min()}"
        )
    else:
        _logger.debug(f"[{stage_name}] No negative values to clip")

    return data


# ---------------------------------------------------------------------------
# Streaming counterparts
#
# ``clip_negative_values`` and ``log_image_stats`` both take ONE array and log ONE line
# computed over the whole of it. A caller that never holds the whole image -- every
# gigapixel-safe reader in this repository -- cannot call them without fragmenting that
# single line into one line per chunk, each with a percentage computed against a chunk
# instead of against the image. Same pixels, different (wrong) observable output.
#
# These two classes fold chunk statistics into the same aggregates and emit the SAME
# line(s), once, at the end. They are the sanctioned way to keep the log contract while
# streaming; do not call the whole-array functions from a chunked loop.
# ---------------------------------------------------------------------------


class StreamingNegativeClip:
    """Accumulates ``clip_negative_values``' whole-image statistics across chunked reads,
    and emits the SAME log line(s) it would have emitted from a single whole-array call --
    once, after the last chunk, not once per chunk.

    ``clip_negative_values`` computes GLOBAL negative-pixel stats (count, percentage of
    the WHOLE image, min before/after) from one array and logs one line. Reading in chunks
    means no single call ever sees the whole array, so calling it per chunk would fragment
    that into N lines, each with a percentage computed against a single chunk's size
    instead of the whole image's. This class keeps the numbers identical to a real
    whole-array call: ``count``/``size`` are summed across chunks and the percentage is
    computed once, at the end, from those sums (never averaged or summed per chunk), and
    ``min_before``/``min_after`` are the true min across all chunks, computed the same way
    a single ``data.min()`` over the whole array would.

    Clipping is applied to every chunk unconditionally (when the dtype is signed/float):
    ``np.clip(x, 0, None)`` is a no-op on already-nonnegative data, so this is
    pixel-identical to the original's whole-array ``if has_neg: data = np.clip(...)``
    regardless of whether the specific chunk -- or even the whole image -- had any
    negative values.

    PASS ONLY REAL PIXELS. A caller that pads its chunks (a partial write-tile at a slide
    edge, say) must hand over the valid sub-array, not the padded buffer: the padding
    would inflate ``size`` and change the reported percentage, which is the one number
    this class exists to keep honest.
    """

    def __init__(self, dtype, logger, stage_name):
        self._logger = logger
        self._stage = stage_name
        self._active = not np.issubdtype(dtype, np.unsignedinteger)
        self._count = 0
        self._size = 0
        self._min_before = None
        self._min_after = None
        if not self._active:
            logger.debug(f"[{stage_name}] Unsigned dtype, no negative clipping needed")

    def process(self, data):
        """Clip one chunk in place-equivalent fashion and fold its stats in."""
        if not self._active:
            return data

        min_before = data.min()
        count = int(np.sum(data < 0))

        self._count += count
        self._size += data.size
        if self._min_before is None or min_before < self._min_before:
            self._min_before = min_before

        clipped = np.clip(data, 0, None)
        min_after = clipped.min()
        if self._min_after is None or min_after < self._min_after:
            self._min_after = min_after

        return clipped

    def finalize(self):
        """Log the aggregate, exactly once, matching detect_negative_values' and
        clip_negative_values' original whole-array log lines."""
        if not self._active:
            return

        if self._count == 0:
            self._logger.debug(f"[{self._stage}] No negative values to clip")
            return

        pct = self._count / self._size * 100
        # Matches detect_negative_values' warning.
        self._logger.warning(
            f"Detected {self._count} negative pixels ({pct:.4f}%), "
            f"min value: {self._min_before}"
        )
        # Matches clip_negative_values' info line.
        self._logger.info(
            f"[{self._stage}] Clipped {self._count} negative pixels ({pct:.4f}%) to 0. "
            f"Min before: {self._min_before}, after: {self._min_after}"
        )


class StreamingImageStats:
    """The chunked counterpart of :func:`log_image_stats`.

    Emits the same three possible lines -- the stats line, the negative-value warning and
    the wrapped-value warning -- from folded per-chunk aggregates. ``dtype`` and ``shape``
    are supplied by the caller because a chunk knows neither the image's full shape nor,
    once it has been cast, necessarily the storage dtype.

    THE MEAN IS ACCUMULATED IN float64, which is what ``ndarray.mean()`` on an integer
    array does anyway (NumPy promotes integer accumulators to float64), so at the one call
    site that matters -- ``bin/apply_basic_profiles.py``, which reports stats on the
    storage-dtype output -- the printed ``mean=...`` is identical to the whole-array call.
    On a float32 input it is *more* accurate than ``ndarray.mean()``'s pairwise float32
    sum, which can only move the value inside the two decimals the format shows.

    PASS ONLY REAL PIXELS, for the same reason :class:`StreamingNegativeClip` requires it:
    padded chunk edges would enter the mean and the wrapped-value percentage.
    """

    def __init__(self, dtype, shape, stage_name, logger=None):
        self._logger = logger or logging.getLogger(__name__)
        self._dtype = np.dtype(dtype)
        self._shape = tuple(shape)
        self._stage = stage_name
        self._min = None
        self._max = None
        self._sum = 0.0
        self._size = 0
        self._high_count = 0

    def process(self, data):
        """Fold one chunk's statistics in. Returns the chunk untouched."""
        if data.size == 0:
            return data
        chunk_min = data.min()
        chunk_max = data.max()
        self._min = chunk_min if self._min is None else min(self._min, chunk_min)
        self._max = chunk_max if self._max is None else max(self._max, chunk_max)
        self._sum += float(np.sum(data, dtype=np.float64))
        self._size += data.size
        if self._dtype == np.uint16:
            self._high_count += int(np.count_nonzero(data > 60000))
        return data

    def finalize(self):
        """Log the aggregate lines, exactly once, matching log_image_stats."""
        if self._size == 0:
            return
        mean_val = self._sum / self._size

        self._logger.info(
            f"[{self._stage}] Image stats: dtype={self._dtype}, shape={self._shape}, "
            f"min={self._min}, max={self._max}, mean={mean_val:.2f}"
        )

        if self._min < 0:
            self._logger.warning(
                f"[{self._stage}] Negative values detected: min={self._min}"
            )

        if self._dtype == np.uint16 and self._max > 60000:
            high_pct = self._high_count / self._size * 100
            if high_pct > 0.1:
                self._logger.warning(
                    f"[{self._stage}] Suspicious high values (potential wrapped "
                    f"negatives): {self._high_count} pixels ({high_pct:.2f}%) > 60000"
                )
