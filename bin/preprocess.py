#!/usr/bin/env python3
"""BaSiC Illumination Correction Preprocessing.

This module provides utilities to apply BaSiC shading correction to large
multichannel images by tiling into FOVs and reconstructing the corrected image.
This version loads a single multichannel image, processes channels in parallel,
and saves the result back to a single TIFF file.
"""

from __future__ import annotations

import argparse
import logging
import math
import os

# Add utils directory to path to import shared modules
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

import numpy as np
import tifffile
from logger import configure_logging, get_logger
from numpy.typing import NDArray
from tiled_io import open_lazy

os.environ["JAX_PLATFORM_NAME"] = "cpu"  # Force CPU for JAX

import basicpy

BASICPY_VERSION = getattr(basicpy, "__version__", "unknown")

from basicpy import BaSiC  # type: ignore  # noqa: E402
from image_utils import ensure_dir  # noqa: E402
from metadata import is_nuclear  # noqa: E402
from pixel_size import (  # noqa: E402
    read_ome_pixel_size,
    warn_on_pixel_size_mismatch,
)
from validation import detect_negative_values, log_image_stats  # noqa: E402

logger = get_logger(__name__)

# Log version info at import time
logger.info(f"BaSiCPy version: {BASICPY_VERSION}")

__all__ = [
    "split_image_into_fovs",
    "reconstruct_image_from_fovs",
    "apply_basic_correction",
    "preprocess_multichannel_image",
]


def count_fovs(
    image_shape: Tuple[int, int], fov_size: Tuple[int, int]
) -> Tuple[int, int]:
    """Calculate how many FOVs are needed to cover an image with a given FOV size.

    Tiling is non-overlapping, matching split_image_into_fovs, which extracts
    adjacent tiles and distributes the remainder pixels across them.
    """
    height, width = image_shape[:2]
    fov_h, fov_w = fov_size

    if fov_h <= 0 or fov_w <= 0:
        raise ValueError(f"FOV size must be positive, got {fov_size}")

    n_fovs_y = math.ceil(height / fov_h)
    n_fovs_x = math.ceil(width / fov_w)

    return n_fovs_y, n_fovs_x


def split_image_into_fovs(
    image: NDArray, n_fovs_x: int, n_fovs_y: int
) -> Tuple[NDArray, List[Tuple[int, int, int, int]], Tuple[int, int]]:
    """
    Split image (H, W) into FOV tiles with adaptive sizing to handle remainders.

    The image is divided into n_fovs_y * n_fovs_x tiles. Remainder pixels are
    distributed across tiles (some tiles get +1 pixel) to exactly cover the image
    without padding.
    """
    if image.ndim != 2:
        raise ValueError(f"Image must be 2D, got shape {image.shape}")

    if n_fovs_x <= 0 or n_fovs_y <= 0:
        raise ValueError("Number of FOVs must be positive")

    height, width = image.shape

    # Calculate base FOV sizes and remainders
    base_w = width // n_fovs_x
    base_h = height // n_fovs_y
    remainder_x = width % n_fovs_x
    remainder_y = height % n_fovs_y

    # Calculate actual FOV sizes (some FOVs get +1 pixel to handle remainders)
    fov_widths = [base_w + (1 if j < remainder_x else 0) for j in range(n_fovs_x)]
    fov_heights = [base_h + (1 if i < remainder_y else 0) for i in range(n_fovs_y)]

    max_w = max(fov_widths)
    max_h = max(fov_heights)

    # Create FOV stack with padding to max dimensions
    n_fovs = n_fovs_y * n_fovs_x
    fov_stack = np.zeros((n_fovs, max_h, max_w), dtype=image.dtype)

    # Extract FOVs and store position info
    positions = []
    y_start = 0
    idx = 0

    for i in range(n_fovs_y):
        x_start = 0
        for j in range(n_fovs_x):
            h = fov_heights[i]
            w = fov_widths[j]

            fov_stack[idx, :h, :w] = image[y_start : y_start + h, x_start : x_start + w]

            positions.append((y_start, x_start, h, w))
            x_start += w
            idx += 1
        y_start += fov_heights[i]

    return fov_stack, positions, (max_h, max_w)


def reconstruct_image_from_fovs(
    fov_stack: NDArray,
    positions: List[Tuple[int, int, int, int]],
    original_shape: Tuple[int, ...],
) -> NDArray:
    """
    Reconstruct 2D image from 3D FOV tiles stack.
    """
    reconstructed = np.zeros(original_shape, dtype=fov_stack.dtype)

    for idx, (row_start, col_start, h, w) in enumerate(positions):
        # fov_stack is 3D (N, fov_h, fov_w), reconstructed is 2D (H, W)
        reconstructed[row_start : row_start + h, col_start : col_start + w] = fov_stack[
            idx, :h, :w
        ]

    return reconstructed


def apply_basic_correction(
    image: NDArray,
    fov_size: Tuple[int, int] = (1950, 1950),
) -> Tuple[NDArray, object]:
    """
    Apply BaSiC illumination correction to a single channel image (H, W).

    BaSiC is run at its own defaults (darkfield estimation on, no autotune).
    `fov_size` is the only knob the pipeline exposes -- see params.preproc_tile_size.
    """
    if image.ndim != 2:
        raise ValueError(
            f"apply_basic_correction requires a 2D image, got shape {image.shape}"
        )

    n_fovs_y, n_fovs_x = count_fovs(image.shape, fov_size)
    fov_stack, positions, _ = split_image_into_fovs(image, n_fovs_x, n_fovs_y)

    basic = BaSiC(get_darkfield=True, smoothness_flatfield=1)

    corrected_fovs = basic.fit_transform(fov_stack)

    # Checkpoint 2: Clip negative values from BaSiC darkfield subtraction
    # BaSiC can produce negative values when darkfield > original pixel value
    has_neg, neg_count, neg_pct = detect_negative_values(corrected_fovs, logger)
    if has_neg:
        logger.warning(
            f"BaSiC correction produced {neg_count} negative pixels ({neg_pct:.4f}%), "
            f"min value: {corrected_fovs.min()}"
        )
        corrected_fovs = np.clip(corrected_fovs, 0, None)
        logger.info(f"Clipped negative values to 0. New min: {corrected_fovs.min()}")

    reconstructed = reconstruct_image_from_fovs(corrected_fovs, positions, image.shape)

    return reconstructed, basic


def _process_single_channel_from_stack(
    channel_image: NDArray,
    channel_index: int,
    channel_name: str,
    fov_size: Tuple[int, int],
    skip_nuclear: bool,
    nuclear_markers: List[str] | None,
) -> Tuple[int, NDArray, bool]:
    """Worker function to process a single channel slice from a stack.

    Which channel counts as nuclear comes from ``nuclear_markers``
    (``params.nuclear_markers``, passed down by PREPROCESS) and is resolved by the
    shared ``utils.metadata.is_nuclear`` rule -- the same one CONVERT_IMAGE and
    ``split_multichannel.py`` use. This used to test ``"DAPI" in name.upper()``
    directly, which silently ignored a configured CELLTOX fiducial: it was BaSiC-
    corrected while DAPI was not, so the channel driving registration and
    segmentation differed depending on which marker a panel happened to use.

    Returns
    -------
    channel_index : int
        Channel index
    processed_image : NDArray
        Corrected or original image
    was_corrected : bool
        Whether BaSiC was applied
    """
    logger.debug(f"Processing channel #{channel_index} ({channel_name})")

    if skip_nuclear and is_nuclear(channel_name, nuclear_markers):
        logger.debug(
            f"  [SKIP] BaSiC correction for nuclear channel {channel_name!r} "
            f"(preproc_skip_nuclear)"
        )
        return channel_index, channel_image, False

    logger.debug("  [OK] Applying BaSiC correction")
    corrected, _ = apply_basic_correction(channel_image, fov_size=fov_size)
    return channel_index, corrected, True


class _StreamingImageStats:
    """Accumulates ``log_image_stats``' whole-array statistics (min, max, mean, and
    the uint16 "suspicious high values" check -- ``bin/utils/validation.py:31-71``)
    across per-channel reads, and emits the SAME log line(s) it would have emitted
    from a single whole-array call -- once, after every channel has been folded in,
    not once per channel and not silently dropped.

    Task 5's lazy read moves the per-channel read into worker threads (see
    ``_read_and_process_channel_lazy``), so no single call ever sees the whole
    stack; ``add_channel`` is called once per channel, from whichever worker thread
    read that channel, and folds its contribution into running totals under a lock.
    ``finalize`` (called from the main thread, after every worker has returned) logs
    the aggregate exactly once.

    min/max/high-count/size are combined exactly -- the same numbers a whole-array
    call would produce, since min/max/counting are associative over a partition of
    the array into channels. mean is computed as
    ``(sum of all channels' float64 sums) / (total element count)``, which is
    numerically the same value NumPy's own ``.mean()`` computes for the same data
    concatenated in any order, up to floating-point summation-order differences of
    at most a few ULP -- invisible at the ``.2f`` precision the log line prints, and
    the same class of approximation Task 3 accepted for the analogous
    ``_StreamingNegativeClip`` in ``bin/split_multichannel.py``.
    """

    def __init__(self, dtype, shape, logger, stage_name):
        self._dtype = dtype
        self._shape = shape
        self._logger = logger
        self._stage = stage_name
        self._lock = threading.Lock()
        self._min = None
        self._max = None
        self._sum = 0.0
        self._size = 0
        self._high_count = 0

    def add_channel(self, channel_data: NDArray) -> None:
        """Fold one channel's statistics into the running totals. Thread-safe: each
        caller passes an array only it has read (see ``_read_and_process_channel_lazy``),
        and the shared totals are updated under a lock.
        """
        c_min = channel_data.min()
        c_max = channel_data.max()
        c_sum = float(np.sum(channel_data, dtype=np.float64))
        c_size = int(channel_data.size)
        c_high = (
            int(np.sum(channel_data > 60000)) if self._dtype == np.uint16 else 0
        )
        with self._lock:
            self._min = c_min if self._min is None else min(self._min, c_min)
            self._max = c_max if self._max is None else max(self._max, c_max)
            self._sum += c_sum
            self._size += c_size
            self._high_count += c_high

    def finalize(self) -> None:
        """Log the aggregate exactly once, matching ``log_image_stats``' whole-array
        log lines (``validation.py:55-71``). No-op if no channel was ever added."""
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


def _read_and_process_channel_lazy(
    image_path: str,
    channel_index: int,
    channel_name: str,
    fov_size: Tuple[int, int],
    skip_nuclear: bool,
    nuclear_markers: List[str] | None,
    stats_agg: "_StreamingImageStats",
) -> Tuple[int, NDArray, bool]:
    """Thread-pool worker entry point for the lazy read path.

    THREAD SAFETY: opens its OWN ``open_lazy`` handle on ``image_path`` -- a fresh
    ``tifffile.imread(..., aszarr=True)`` store, never one shared with any other
    thread or with the main thread -- reads exactly one channel plane, and closes
    that handle before doing anything else. tifffile's zarr view is not documented
    thread-safe for concurrent region reads through a SHARED store; giving every
    worker its own store sidesteps that question by construction, since no
    mutable I/O state is ever shared across threads. This is exercised directly by
    ``tests/test_preprocess_lazy_concurrency.py``: the real ``ThreadPoolExecutor``
    path, at real concurrency, run many times against known per-channel ground
    truth -- a proof of "this specific design didn't corrupt output over N runs",
    not a proof that no race can ever occur.
    """
    lazy_arr, _lazy_dtype, close_lazy = open_lazy(image_path)
    try:
        channel_image = np.asarray(lazy_arr[channel_index, :, :])
    finally:
        close_lazy()

    stats_agg.add_channel(channel_image)

    return _process_single_channel_from_stack(
        channel_image, channel_index, channel_name, fov_size, skip_nuclear, nuclear_markers
    )


def preprocess_multichannel_image(
    image_path: str,
    channel_names: List[str],
    output_path: str,
    fov_size: Tuple[int, int] = (1950, 1950),
    skip_nuclear: bool = True,
    nuclear_markers: List[str] | None = None,
    n_workers: int = 4,
    pixel_size: float = 0.325,
) -> NDArray:
    """
    Apply BaSiC preprocessing to a single multichannel image in parallel and save as TIFF.
    """
    logger.info(f"Loading multichannel image from {image_path}")

    # Read and log input file metadata.
    #
    # The scale is read through the shared rule in utils/pixel_size.py rather than
    # parsed here: this module used to carry its own OME-XML walk with its own hardcoded
    # 0.325 fallback, which meant a run with --pixel_size set to anything else silently
    # fell back to a number nothing in the run had asked for.
    detected_x, detected_y = read_ome_pixel_size(image_path)
    warn_on_pixel_size_mismatch(
        (detected_x, detected_y),
        pixel_size,
        source=Path(image_path).name,
        logger=logger,
    )
    # What gets stamped on the preprocessed intermediate is what the input claimed, so
    # the provenance survives; the configured value is the fallback, and it is what every
    # downstream µm conversion uses regardless (see utils/pixel_size.py).
    pixel_size_x = detected_x if detected_x is not None else pixel_size
    pixel_size_y = detected_y if detected_y is not None else pixel_size
    if detected_x is not None:
        logger.info(
            f"  Pixel size from input OME metadata: X={pixel_size_x}, Y={pixel_size_y} µm"
        )
    with tifffile.TiffFile(image_path) as tif:
        if tif.pages and len(tif.pages) > 0:
            first_page = tif.pages[0]
            logger.debug(
                f"  First page: shape={first_page.shape}, dtype={first_page.dtype}"
            )
        # Series-level shape/ndim/dtype is metadata only -- it does not decode any
        # pixel data (same guarantee tifffile.imread(..., aszarr=True) relies on) --
        # so this probe replaces the old eager tifffile.imread(image_path) for
        # everything that only needs the RAW on-disk shape (2-D vs 3-D, channel-last
        # detection), before any 2-D-expand / channel-last-transpose logic runs.
        series0 = tif.series[0]
        raw_shape = tuple(series0.shape)
        raw_ndim = series0.ndim
        stack_dtype = series0.dtype

    logger.info(f"Loaded image shape: {raw_shape}")

    # (Y, X, C) on disk: needs two (or more) planes at once to decide whether the
    # data is genuinely multichannel or greyscale stored as RGB -- not separable
    # per-channel the way the rest of this function is, and never produced by this
    # pipeline's own writers (CONVERT_IMAGE / this module always emit axes="CYX").
    # Task 5 keeps this branch on the exact pre-change eager path -- same
    # whole-stack tifffile.imread, same log_image_stats call on the pre-transpose
    # array -- rather than force it through open_lazy (whose (C, H, W) contract
    # assumes channel-FIRST). See generate_preprocess_qc.py / split_multichannel.py
    # for the same fallback applied at Tasks 2/4's sites.
    channel_last = (
        raw_ndim == 3
        and raw_shape[2] == len(channel_names)
        and raw_shape[0] != len(channel_names)
    )

    if channel_last:
        logger.debug("  Loading whole stack eagerly (channel-last layout on disk)")
        multichannel_stack = tifffile.imread(image_path)

        # Checkpoint 1: Log input image statistics (whole-array, pre-transpose --
        # matches the pre-change call exactly).
        log_image_stats(multichannel_stack, "input", logger)

        logger.debug("  Transposing from (Y, X, C) to (C, Y, X)")
        multichannel_stack = np.transpose(multichannel_stack, (2, 0, 1))
        n_channels, H, W = multichannel_stack.shape
        stack_dtype = multichannel_stack.dtype
    else:
        # 2-D on disk is presented as C=1 (matching the old np.expand_dims(...,
        # axis=0) branch); channel-first 3-D on disk needs no reshaping at all --
        # either way nothing here has to touch pixel data.
        if raw_ndim == 2:
            n_channels, H, W = 1, raw_shape[0], raw_shape[1]
        else:
            n_channels, H, W = raw_shape

        # Checkpoint 1: Log input image statistics -- streamed across the
        # per-channel reads below (each happens inside its OWN worker thread, its
        # OWN open_lazy handle: see _read_and_process_channel_lazy) and aggregated
        # to the SAME numbers a single whole-array log_image_stats call would have
        # produced. See _StreamingImageStats.
        stats_agg = _StreamingImageStats(stack_dtype, raw_shape, logger, "input")

    logger.info(f"Processing {n_channels} channels ({H}x{W}) with {n_workers} workers")

    if n_channels != len(channel_names):
        if n_channels < len(channel_names):
            logger.warning(
                f"Image has {n_channels} channels but {len(channel_names)} channel names provided. Truncating channel names."
            )
        channel_names = channel_names[:n_channels] + [
            f"Channel_{i}" for i in range(len(channel_names), n_channels)
        ]

    results = {}
    correction_applied = {}

    if channel_last:
        # Whole stack already resident in memory (see above) -- no benefit to a
        # lazy per-thread read here, so each worker gets its pre-sliced channel
        # exactly as the pre-change code did.
        def _channel_worker(i: int):
            return _process_single_channel_from_stack(
                multichannel_stack[i, ...],
                i,
                channel_names[i],
                fov_size,
                skip_nuclear,
                nuclear_markers,
            )
    else:
        # THREAD SAFETY: each call opens and closes its OWN open_lazy handle inside
        # the worker thread that runs it -- see _read_and_process_channel_lazy's
        # docstring and tests/test_preprocess_lazy_concurrency.py.
        def _channel_worker(i: int):
            return _read_and_process_channel_lazy(
                str(image_path),
                i,
                channel_names[i],
                fov_size,
                skip_nuclear,
                nuclear_markers,
                stats_agg,
            )

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(_channel_worker, i) for i in range(n_channels)]

        for future in as_completed(futures):
            channel_index, result_array, was_corrected = future.result()
            results[channel_index] = result_array
            correction_applied[channel_index] = was_corrected

    if not channel_last:
        stats_agg.finalize()

    preprocessed_channels = [results[i] for i in range(n_channels)]

    # Log summary
    n_corrected = sum(correction_applied.values())
    n_skipped = n_channels - n_corrected
    logger.info(
        f"BaSiC Correction Summary: corrected={n_corrected}/{n_channels}, skipped={n_skipped}/{n_channels}"
    )

    preprocessed = np.stack(preprocessed_channels, axis=0)

    # Cast back to original dtype (BaSiC outputs float32, but downstream expects uint16)
    if preprocessed.dtype != stack_dtype:
        logger.info(f"Casting from {preprocessed.dtype} back to {stack_dtype}")
        preprocessed = (
            np.clip(
                preprocessed,
                np.iinfo(stack_dtype).min,
                np.iinfo(stack_dtype).max,
            )
            if np.issubdtype(stack_dtype, np.integer)
            else preprocessed
        )
        if np.issubdtype(stack_dtype, np.integer):
            # Round to nearest (half-to-even) before casting so fractional BaSiC
            # output doesn't get silently truncated toward zero (astype floors).
            preprocessed = np.round(preprocessed)
        preprocessed = preprocessed.astype(stack_dtype)

    # Log output statistics after all processing
    log_image_stats(preprocessed, "after_basic_correction", logger)

    logger.info(f"Saving corrected image to {output_path}")
    logger.debug(
        f"  Final stack shape: {preprocessed.shape}, channels: {channel_names[: preprocessed.shape[0]]}"
    )

    # Save as OME-TIFF with proper metadata
    # VALIS expects OME-TIFF with proper channel dimension and physical size metadata
    # PhysicalSize is whatever the input claimed, falling back to the configured
    # --pixel-size. VALIS reads this off the slide, so it has to be present.
    metadata = {
        "axes": "CYX",
        "Channel": {"Name": channel_names[: preprocessed.shape[0]]},
        "PhysicalSizeX": pixel_size_x,
        "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": pixel_size_y,
        "PhysicalSizeYUnit": "µm",
    }

    logger.debug(
        f"  OME metadata: axes={metadata['axes']}, pixel_size={pixel_size_x}x{pixel_size_y}µm"
    )

    tifffile.imwrite(
        output_path,
        preprocessed,
        photometric="minisblack",
        metadata=metadata,
        bigtiff=True,
        ome=True,
        compression="zlib",
        tile=(2048, 2048),
    )

    logger.info(f"[OK] Saved OME-TIFF with {preprocessed.shape[0]} channels")

    # Verify the saved file
    with tifffile.TiffFile(output_path) as tif:
        has_ome = hasattr(tif, "ome_metadata") and tif.ome_metadata
        if not has_ome:
            logger.warning("[WARN] No OME metadata in saved file")

    # CHECKPOINT 1: Verify preprocessed data has no negatives (use in-memory array, no re-read)
    logger.info("[CHECKPOINT 1] Verifying preprocessed data...")
    verify_min = float(preprocessed.min())
    verify_max = float(preprocessed.max())
    if verify_min < 0:
        neg_count = int(np.sum(preprocessed < 0))
        neg_pct = 100 * neg_count / preprocessed.size
        logger.error(
            f"[CHECKPOINT 1 FAIL] Preprocessed data has {neg_count} negatives ({neg_pct:.4f}%), min={verify_min}"
        )
    else:
        logger.info(
            f"[CHECKPOINT 1 OK] No negatives. min={verify_min:.2f}, max={verify_max:.2f}"
        )

    return preprocessed


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Apply BaSiC illumination correction to multichannel images",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--image", type=str, required=True, help="Path to the multichannel image file"
    )

    parser.add_argument(
        "--output_dir", type=str, required=True, help="Output directory"
    )

    parser.add_argument(
        "--channels",
        type=str,
        nargs="+",
        required=True,
        help="Channel names from metadata",
    )

    parser.add_argument(
        "--fov_size", type=int, default=1950, help="FOV size for BaSiC tiling"
    )

    parser.add_argument(
        "--n_workers",
        type=int,
        default=4,
        help="Maximum number of channels to process in parallel.",
    )

    parser.add_argument(
        "--skip_nuclear",
        action="store_true",
        help="Skip BaSiC correction on the nuclear/fiducial channels named by "
        "--nuclear-markers",
    )

    parser.add_argument(
        "--nuclear-markers",
        dest="nuclear_markers",
        nargs="+",
        default=None,
        help="Ordered nuclear/fiducial marker names. PREPROCESS passes "
        "params.nuclear_markers; the default is only for standalone use.",
    )

    parser.add_argument(
        "--pixel-size",
        dest="pixel_size",
        type=float,
        default=0.325,
        help="Configured scale in µm/px (PREPROCESS passes params.pixel_size). Used as "
        "the fallback when the input carries no OME scale, and as the value the "
        "input's own scale is checked against.",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)

    args = parse_args()

    ensure_dir(args.output_dir)

    image_path = args.image
    image_basename = os.path.basename(image_path)

    channel_names = args.channels

    # Always save as .ome.tiff since we're writing OME-TIFF format
    if image_basename.endswith(".ome.tif"):
        base = image_basename[:-8]  # Remove .ome.tif
    elif image_basename.endswith(".ome.tiff"):
        base = image_basename[:-9]  # Remove .ome.tiff
    elif image_basename.endswith(".tif"):
        base = image_basename[:-4]  # Remove .tif
    elif image_basename.endswith(".tiff"):
        base = image_basename[:-5]  # Remove .tiff
    else:
        base = os.path.splitext(image_basename)[0]

    ext = ".ome.tif"  # Always use OME-TIFF extension
    output_filename = f"{base}_corrected{ext}"
    output_path = os.path.join(args.output_dir, output_filename)

    logger.info(f"Starting preprocessing: {image_path}")
    logger.info(f"Expected channel order: {channel_names}")

    preprocessed = preprocess_multichannel_image(
        image_path=image_path,
        channel_names=channel_names,
        output_path=output_path,
        fov_size=(args.fov_size, args.fov_size),
        skip_nuclear=args.skip_nuclear,
        nuclear_markers=args.nuclear_markers,
        n_workers=args.n_workers,
        pixel_size=args.pixel_size,
    )

    logger.info(f"Preprocessing completed successfully. Output: {output_path}")

    # Write dimensions to file for downstream processes (use in-memory shape, no re-read)
    shape = (
        preprocessed.shape
        if preprocessed.ndim == 3
        else (1, preprocessed.shape[0], preprocessed.shape[1])
    )
    dims_filename = f"{base}_dims.txt"
    dims_path = os.path.join(args.output_dir, dims_filename)
    with open(dims_path, "w") as f:
        f.write(f"{shape[0]} {shape[1]} {shape[2]}")
    logger.info(
        f"Image dimensions saved to: {dims_path} (C={shape[0]}, H={shape[1]}, W={shape[2]})"
    )

    return 0


if __name__ == "__main__":
    exit(main())
