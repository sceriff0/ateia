#!/usr/bin/env python3
"""Generate preprocessing quality control images.

This script creates PNG images for each channel in a preprocessed multichannel
OME-TIFF image for fast visual inspection of preprocessing results.

Examples
--------
Generate QC PNGs for a preprocessed image:

    $ python generate_preprocess_qc.py \\
        --image patient1_corrected.ome.tif \\
        --output qc/ \\
        --channels DAPI CD4 CD8

Custom scale factor:

    $ python generate_preprocess_qc.py \\
        --image patient1_corrected.ome.tif \\
        --output qc/ \\
        --channels DAPI CD4 CD8 \\
        --scale-factor 0.1

Output Format
-------------
For each channel, creates:
- {patient_id}_{channel_name}.png: Downsampled grayscale PNG with auto-contrast

Notes
-----
The images are contrast-adjusted using percentile-based normalization
(1st to 99th percentile) for optimal visualization.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import tifffile
from skimage.io import imsave
from skimage.transform import resize

# Add utils directory to path
sys.path.insert(0, str(Path(__file__).parent / "utils"))

from logger import configure_logging, get_logger
from tiled_io import open_lazy, read_decimated

__all__ = ["main", "generate_preprocess_qc"]


def normalize_image(
    image: np.ndarray, percentile_low: float = 1.0, percentile_high: float = 99.0
) -> np.ndarray:
    """Normalize image to 0-255 range using percentile-based contrast.

    Parameters
    ----------
    image : np.ndarray
        Input 2D image (any dtype).
    percentile_low : float
        Lower percentile for contrast adjustment (default: 1.0).
    percentile_high : float
        Upper percentile for contrast adjustment (default: 99.0).

    Returns
    -------
    np.ndarray
        Normalized image as uint8 (0-255).
    """
    img = image.astype(np.float32)

    # Calculate percentiles for contrast adjustment
    p_low = np.percentile(img, percentile_low)
    p_high = np.percentile(img, percentile_high)

    # Avoid division by zero
    if p_high - p_low < 1e-6:
        p_high = p_low + 1.0

    # Normalize to 0-1 range
    img = (img - p_low) / (p_high - p_low)
    img = np.clip(img, 0, 1)

    # Convert to 8-bit. ROUND before casting -- `.astype()` floors, which biases every pixel
    # down by up to one level and, worse, starves the top bin: only an input of exactly 1.0
    # would reach 255, so the brightest tissue reads one level dark on every QC render.
    # Matches the half-to-even convention stated at bin/preprocess.py:354.
    # Guarded by tests/test_dtype_rounding_contract.py.
    return np.round(img * 255).astype(np.uint8)


def downsample_image(image: np.ndarray, scale_factor: float) -> np.ndarray:
    """Downsample image by a given scale factor.

    Parameters
    ----------
    image : np.ndarray
        Input 2D image.
    scale_factor : float
        Scale factor (e.g., 0.25 for 4x downsampling).

    Returns
    -------
    np.ndarray
        Downsampled image.
    """
    if scale_factor >= 1.0:
        return image

    new_shape = (int(image.shape[0] * scale_factor), int(image.shape[1] * scale_factor))
    # Use anti_aliasing for better downsampling quality
    downsampled = resize(
        image,
        new_shape,
        order=1,  # Bilinear interpolation
        anti_aliasing=True,
        preserve_range=True,
    )
    return downsampled.astype(image.dtype)


def generate_preprocess_qc(
    image_path: Path,
    output_dir: Path,
    channel_names: List[str],
    scale_factor: float = 0.25,
    prefix: Optional[str] = None,
    percentile_low: float = 1.0,
    percentile_high: float = 99.0,
    logger: Optional[logging.Logger] = None,
) -> List[Path]:
    """Generate QC PNG images for each channel in a preprocessed image.

    Parameters
    ----------
    image_path : Path
        Path to preprocessed OME-TIFF image.
    output_dir : Path
        Output directory for PNG files.
    channel_names : List[str]
        List of channel names.
    scale_factor : float
        Downsampling factor (default: 0.25 = 4x smaller).
    prefix : Optional[str]
        Prefix for output filenames. If None, uses image basename.
    percentile_low : float
        Lower percentile for contrast (default: 1.0).
    percentile_high : float
        Upper percentile for contrast (default: 99.0).
    logger : Optional[logging.Logger]
        Logger instance.

    Returns
    -------
    List[Path]
        List of generated PNG file paths.
    """
    if logger is None:
        logger = get_logger(__name__)

    # Open the image lazily (zarr-backed view) and read/normalize/downsample one
    # channel at a time below, instead of tifffile.imread'ing the whole (C, H, W)
    # stack up front. This is a peak-MEMORY optimization only, not a read-size
    # optimization: downsample_image's anti-aliased resize (skimage.transform.resize,
    # order=1, anti_aliasing=True) needs every source pixel by definition, so there is
    # no I/O to save here -- read_decimated is called with factor=1 below, meaning
    # every pixel of every channel is still read, at full resolution, exactly as
    # before. What changes is that peak memory becomes one full-resolution plane plus
    # the downsampled outputs, never the whole multi-channel stack at once.
    logger.info(f"Opening image lazily: {image_path}")
    lazy_arr, _dtype, close = open_lazy(image_path)
    closed = False
    try:
        # Handle (Y, X, C) format — only true if last dim matches channels AND first
        # dim does not. open_lazy always presents the array channel-FIRST (a 2-D image
        # is promoted to C=1, matching the old np.expand_dims(..., axis=0) handling),
        # so it has no notion of a channel-LAST layout. That shape is never produced by
        # this pipeline's own preprocess.py (always written with axes="CYX"), but the
        # old whole-stack path is kept as an exact-behavior fallback for it.
        channel_last = (
            len(lazy_arr.shape) == 3
            and lazy_arr.shape[2] == len(channel_names)
            and lazy_arr.shape[0] != len(channel_names)
        )

        if channel_last:
            close()
            closed = True
            logger.info(f"Loading image (channel-last layout): {image_path}")
            img_stack = tifffile.imread(image_path)
            if img_stack.ndim == 2:
                img_stack = np.expand_dims(img_stack, axis=0)
            logger.info("Transposing from (Y, X, C) to (C, Y, X)")
            img_stack = np.transpose(img_stack, (2, 0, 1))
            logger.info(f"Image shape: {img_stack.shape}")
            n_channels = img_stack.shape[0]

            def read_channel(i: int) -> np.ndarray:
                return img_stack[i]
        else:
            logger.info(f"Image shape: {lazy_arr.shape}")
            n_channels = lazy_arr.shape[0]

            def read_channel(i: int) -> np.ndarray:
                return read_decimated(lazy_arr, i, factor=1)

        logger.info(f"Processing {n_channels} channels")

        # Adjust channel names if needed
        if len(channel_names) < n_channels:
            channel_names = channel_names + [
                f"Channel_{i}" for i in range(len(channel_names), n_channels)
            ]
        channel_names = channel_names[:n_channels]

        # Determine output prefix
        if prefix is None:
            # Extract basename, removing common suffixes
            prefix = image_path.stem
            for suffix in [".ome", "_corrected", "_preprocessed"]:
                if prefix.endswith(suffix):
                    prefix = prefix[: -len(suffix)]

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate PNGs for each channel
        output_files = []
        for i, channel_name in enumerate(channel_names):
            logger.info(f"Processing channel {i + 1}/{n_channels}: {channel_name}")

            # Read one channel at a time (see above)
            channel_img = read_channel(i)

            # Normalize for visualization
            normalized = normalize_image(
                channel_img, percentile_low=percentile_low, percentile_high=percentile_high
            )

            # Downsample
            downsampled = downsample_image(normalized, scale_factor)

            # Save PNG
            output_path = output_dir / f"{prefix}_{channel_name}.png"
            imsave(str(output_path), downsampled, check_contrast=False)

            logger.info(
                f"  ✓ Saved: {output_path.name} ({downsampled.shape[1]}x{downsampled.shape[0]})"
            )
            output_files.append(output_path)

        return output_files
    finally:
        if not closed:
            close()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate preprocessing QC images (PNG per channel)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate QC PNGs for a preprocessed image
  %(prog)s \\
    --image patient1_corrected.ome.tif \\
    --output qc/ \\
    --channels DAPI CD4 CD8

  # Custom scale factor for larger previews
  %(prog)s \\
    --image patient1_corrected.ome.tif \\
    --output qc/ \\
    --channels DAPI CD4 CD8 \\
    --scale-factor 0.5

Output:
  For each channel, creates:
    - {prefix}_{channel_name}.png  (downsampled, contrast-adjusted)
        """,
    )

    parser.add_argument(
        "--image", type=Path, required=True, help="Path to preprocessed OME-TIFF image"
    )

    parser.add_argument(
        "--output", type=Path, required=True, help="Output directory for PNG files"
    )

    parser.add_argument(
        "--channels", type=str, nargs="+", required=True, help="Channel names"
    )

    parser.add_argument(
        "--scale-factor",
        type=float,
        default=0.25,
        help="Downsampling factor (default: 0.25 = 4x smaller)",
    )

    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="Prefix for output filenames (default: image basename)",
    )

    parser.add_argument(
        "--percentile-low",
        type=float,
        default=1.0,
        help="Lower percentile for contrast adjustment (default: 1.0)",
    )

    parser.add_argument(
        "--percentile-high",
        type=float,
        default=99.0,
        help="Upper percentile for contrast adjustment (default: 99.0)",
    )

    parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose (DEBUG) logging"
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point for preprocessing QC generation.

    Returns
    -------
    int
        Exit code (0 for success, 1 for failure).
    """
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    configure_logging(
        level=log_level, format_string="[%(asctime)s] %(levelname)s - %(message)s"
    )

    logger = get_logger(__name__)

    logger.info("=" * 80)
    logger.info("PREPROCESSING QC GENERATION")
    logger.info("=" * 80)

    # Validate input
    if not args.image.exists():
        logger.error(f"Image not found: {args.image}")
        return 1

    logger.info(f"Input image: {args.image}")
    logger.info(f"Output directory: {args.output}")
    logger.info(f"Channels: {args.channels}")
    logger.info(f"Scale factor: {args.scale_factor}")
    logger.info("")

    try:
        output_files = generate_preprocess_qc(
            image_path=args.image,
            output_dir=args.output,
            channel_names=args.channels,
            scale_factor=args.scale_factor,
            prefix=args.prefix,
            percentile_low=args.percentile_low,
            percentile_high=args.percentile_high,
            logger=logger,
        )

        logger.info("")
        logger.info("=" * 80)
        logger.info("QC GENERATION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"✓ Generated {len(output_files)} PNG files")
        for f in output_files:
            logger.info(f"  - {f.name}")
        logger.info("=" * 80)
        return 0

    except Exception as e:
        logger.error(f"Failed to generate QC: {e}")
        logger.debug("Full traceback:", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
