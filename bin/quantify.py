#!/usr/bin/env python3
"""Cell quantification for single-channel processing.

Designed for Nextflow pipelines where each channel is processed separately
and results are merged afterward. Supports both CPU and GPU processing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from numpy.typing import NDArray

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

# Import from lib for DRY principle
from logger import get_logger, configure_logging
from image_utils import ensure_dir, load_image

logger = get_logger(__name__)


__all__ = [
    "compute_channel_intensity",
    "quantify_single_channel",
    "run_quantification",
]


def compute_channel_intensity(
    mask_filtered: NDArray,
    channel: NDArray,
    valid_labels: NDArray,
    channel_name: str
) -> pd.Series:
    """Compute mean intensity per cell for a single channel.

    Parameters
    ----------
    mask_filtered : ndarray, shape (Y, X)
        Pre-filtered segmentation mask with valid cell labels only.
    channel : ndarray, shape (Y, X)
        Channel intensity image (same spatial dimensions as mask).
    valid_labels : ndarray
        Array of valid cell label IDs to compute intensities for.
    channel_name : str
        Name of the channel/marker for the output Series.

    Returns
    -------
    pd.Series
        Mean intensities indexed by cell label, with name=channel_name.
        Missing or out-of-bounds labels are assigned intensity 0.0.

    Notes
    -----
    This function uses np.bincount for efficient vectorized computation
    of mean intensities. It handles edge cases like missing labels or
    labels outside the computed range.

    The mean intensity is computed as the sum of pixel intensities
    divided by the number of pixels for each cell.

    Examples
    --------
    >>> mask = np.array([[1, 1, 0], [1, 0, 2], [0, 2, 2]])
    >>> channel = np.array([[10, 20, 0], [30, 0, 40], [0, 50, 60]])
    >>> valid_labels = np.array([1, 2])
    >>> intensities = compute_channel_intensity(mask, channel, valid_labels, "CD3")
    >>> print(intensities)
    1    20.0
    2    50.0
    Name: CD3, dtype: float64

    See Also
    --------
    compute_morphology : Compute morphological properties
    quantify_single_channel : Complete quantification pipeline
    """
    channel = channel.squeeze()
    
    # Efficient bincount-based mean computation
    flat_mask = mask_filtered.ravel()
    flat_channel = channel.ravel().astype(np.float64)

    sum_per_label = np.bincount(flat_mask, weights=flat_channel)
    count_per_label = np.bincount(flat_mask)

    # Compute means with safe division
    with np.errstate(divide='ignore', invalid='ignore'):
        mean_intensities = np.divide(sum_per_label, count_per_label)
        mean_intensities = np.nan_to_num(mean_intensities, nan=0.0)

    # Vectorized extraction
    max_label = len(mean_intensities) - 1
    safe_labels = np.clip(valid_labels, 0, max_label)
    intensities = mean_intensities[safe_labels]
    
    # Handle any labels that were out of bounds
    out_of_bounds = valid_labels > max_label
    if np.any(out_of_bounds):
        intensities[out_of_bounds] = 0.0

    return pd.Series(intensities, index=valid_labels, name=channel_name)


def quantify_single_channel(
    mask: NDArray,
    channel_image: NDArray,
    channel_name: str,
    min_area: int = 0
) -> pd.DataFrame:
    """Quantify a single channel (intensity only).

    Morphology is computed separately by EXTRACT_CELL_PROPERTIES.
    This function only computes mean intensity per cell.

    Parameters
    ----------
    mask : ndarray, shape (Y, X)
        Segmentation mask.
    channel_image : ndarray, shape (Y, X)
        Channel intensity image.
    channel_name : str
        Name of the channel/marker.
    min_area : int, optional
        Minimum cell area filter.

    Returns
    -------
    DataFrame
        Intensity per cell with columns: label, {channel_name}.
    """
    mask = np.ascontiguousarray(mask.squeeze())

    # Filter cells by area (same logic as extract_cell_properties)
    labels, counts = np.unique(mask, return_counts=True)
    valid_labels = labels[(labels != 0) & (counts >= min_area)]

    if len(valid_labels) == 0:
        logger.warning("[WARN] No valid cells found after area filtering")
        return pd.DataFrame()

    mask_filtered = np.where(np.isin(mask, valid_labels), mask, 0)

    if np.all(mask_filtered == 0):
        logger.warning("[WARN] Filtered mask is empty")
        return pd.DataFrame()

    # Compute intensity for this channel (no morphology — that's done once upstream)
    intensity_series = compute_channel_intensity(
        mask_filtered, channel_image, valid_labels, channel_name
    )

    result_df = pd.DataFrame({
        'label': valid_labels,
        channel_name: intensity_series.values,
    })

    return result_df


def run_quantification(
    mask_path: str,
    channel_path: str,
    output_path: str,
    min_area: int = 0,
    channel_name: str = None
) -> pd.DataFrame:
    """Run quantification for a single channel.

    Parameters
    ----------
    mask_path : str
        Path to segmentation mask (.npy or .tif file).
    channel_path : str
        Path to channel image file (.tif).
    output_path : str
        Path to save output CSV file.
    min_area : int, default=0
        Minimum cell area filter in pixels. Cells smaller than this
        are excluded from quantification.
    channel_name : str, optional
        Explicit channel/marker name. If not provided, will be parsed
        from the channel file basename.

    Returns
    -------
    pd.DataFrame
        Quantification results with columns: label, {channel_name}.
        Morphology is computed separately by EXTRACT_CELL_PROPERTIES.

    Raises
    ------
    FileNotFoundError
        If mask_path or channel_path does not exist.
    ValueError
        If mask and channel have incompatible shapes.
    """
    # Use provided channel name, or extract from filename as fallback
    if channel_name is None:
        channel_name = Path(channel_path).stem.split('_')[-1]
        logger.info(f"Channel name parsed from filename: {channel_name}")
    
    logger.info("=" * 60)
    logger.info(f"Quantifying channel: {channel_name}")
    logger.info("=" * 60)

    # Load segmentation mask
    logger.info(f"Loading mask: {mask_path}")
    try:
        if mask_path.endswith('.npy'):
            mask = np.load(mask_path).squeeze()
        else:
            mask, _ = load_image(mask_path)
            mask = mask.squeeze()
        logger.debug(f"  Mask shape: {mask.shape}")
    except FileNotFoundError:
        logger.error(f"[FAIL] Segmentation mask not found: {mask_path}")
        raise FileNotFoundError(f"Segmentation mask not found: {mask_path}")
    except Exception as e:
        logger.error(f"[FAIL] Failed to load mask from {mask_path}: {e}")
        raise ValueError(f"Failed to load mask from {mask_path}: {e}") from e

    # Load channel image
    logger.info(f"Loading channel: {channel_path}")
    try:
        channel_image, _ = load_image(channel_path)
        logger.debug(f"  Channel shape: {channel_image.shape}")
    except FileNotFoundError:
        logger.error(f"[FAIL] Channel image not found: {channel_path}")
        raise FileNotFoundError(f"Channel image not found: {channel_path}")
    except Exception as e:
        logger.error(f"[FAIL] Failed to load channel from {channel_path}: {e}")
        raise ValueError(f"Failed to load channel from {channel_path}: {e}") from e

    # Validate shapes match
    if mask.shape != channel_image.squeeze().shape:
        logger.error(
            f"[FAIL] Shape mismatch: mask {mask.shape} vs channel {channel_image.squeeze().shape}"
        )
        raise ValueError(
            f"Shape mismatch: mask {mask.shape} vs channel {channel_image.squeeze().shape}. "
            f"Ensure the mask and channel images have the same spatial dimensions."
        )

    # Quantify
    result_df = quantify_single_channel(
        mask, channel_image, channel_name, min_area=min_area
    )

    # Save
    if not result_df.empty:
        result_df.to_csv(output_path, index=False)
        logger.info(f"[OK] Saved {len(result_df)} cells to {output_path}")
    else:
        logger.warning("[WARN] No results to save")
        # Create empty CSV with expected columns (intensity-only)
        empty_df = pd.DataFrame(columns=['label', channel_name])
        empty_df.to_csv(output_path, index=False)

    logger.info("Quantification complete")

    return result_df


def run_quantification_gpu(
    mask_path: str,
    channel_path: str,
    output_path: str,
    min_area: int = 0,
    channel_name: str = None
) -> pd.DataFrame:
    """Run GPU-accelerated quantification for a single channel.

    Parameters
    ----------
    mask_path : str
        Path to segmentation mask (.npy file).
    channel_path : str
        Path to channel image file (.tif).
    output_path : str
        Path to save output CSV.
    min_area : int, optional
        Minimum cell area filter.
    channel_name : str, optional
        Explicit channel name. If not provided, will parse from filename.

    Returns
    -------
    DataFrame
        Quantification results.
    """
    import cupy as cp
    from cucim.skimage.measure import regionprops_table as gpu_regionprops_table

    # Use provided channel name, or extract from filename as fallback
    if channel_name is None:
        channel_name = Path(channel_path).stem.split('_')[-1]
        logger.info(f"Channel name parsed from filename: {channel_name}")
    
    logger.info("=" * 60)
    logger.info(f"Quantifying channel (GPU): {channel_name}")
    logger.info("=" * 60)

    # Load data
    logger.info(f"Loading mask: {mask_path}")
    if mask_path.endswith('.npy'):
        segmentation_mask = np.load(mask_path).squeeze()
    else:
        segmentation_mask, _ = load_image(mask_path)
        segmentation_mask = segmentation_mask.squeeze()
    
    logger.info(f"Loading channel: {channel_path}")
    channel_image, _ = load_image(channel_path)
    channel_image = channel_image.squeeze()

    # Validate shapes
    if segmentation_mask.shape != channel_image.shape:
        raise ValueError(
            f"Shape mismatch: mask {segmentation_mask.shape} vs channel {channel_image.shape}"
        )

    # Transfer to GPU
    logger.info("Transferring to GPU...")
    mask_gpu = cp.asarray(segmentation_mask)
    channel_gpu = cp.asarray(channel_image)

    # Filter by area
    labels, counts = cp.unique(mask_gpu, return_counts=True)
    valid_labels = labels[(labels != 0) & (counts >= min_area)]

    if len(valid_labels) == 0:
        logger.warning("No valid cells found")
        empty_df = pd.DataFrame(columns=['label', channel_name])
        empty_df.to_csv(output_path, index=False)
        return empty_df

    # Filter mask
    mask_filtered = cp.where(cp.isin(mask_gpu, valid_labels), mask_gpu, 0)

    # Compute intensity only (morphology is done by EXTRACT_CELL_PROPERTIES)
    logger.info("Computing intensity (GPU)...")
    flat_mask = mask_filtered.ravel()
    flat_channel = channel_gpu.ravel().astype(cp.float64)

    sum_per_label = cp.bincount(flat_mask, weights=flat_channel)
    count_per_label = cp.bincount(flat_mask)

    means = cp.where(count_per_label != 0, sum_per_label / count_per_label, 0.0)
    intensities = means[valid_labels]

    # Create result dataframe (intensity-only)
    result_df = pd.DataFrame({
        'label': valid_labels.get(),
        channel_name: intensities.get(),
    })

    # Save
    logger.info(f"Saving: {output_path}")
    result_df.to_csv(output_path, index=False)
    logger.info(f"Cells: {len(result_df)}")

    # Cleanup GPU memory
    del mask_gpu, channel_gpu, mask_filtered, flat_mask, flat_channel
    cp.get_default_memory_pool().free_all_blocks()

    return result_df


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Single-channel cell quantification (Nextflow compatible)',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        '--channel_tiff',
        type=str,
        required=True,
        help='Path to single channel TIFF image'
    )
    parser.add_argument(
        '--mask_file',
        type=str,
        required=True,
        help='Path to segmentation mask (.npy)'
    )
    parser.add_argument(
        '--outdir',
        type=str,
        default='.',
        help='Output directory'
    )
    parser.add_argument(
        '--output_file',
        type=str,
        help='Output CSV filename (default: {channel}_quant.csv)'
    )
    parser.add_argument(
        '--min_area',
        type=int,
        default=0,
        help='Minimum cell area (pixels)'
    )
    parser.add_argument(
        '--channel-name',
        type=str,
        default=None,
        help='Explicit channel name (if not provided, will parse from filename)'
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)

    args = parse_args()

    # Ensure output directory exists
    ensure_dir(args.outdir)

    # Derive effective channel name once for consistency
    effective_channel_name = args.channel_name or Path(args.channel_tiff).stem.split('_')[-1]

    # Determine output path
    if args.output_file:
        output_path = os.path.join(args.outdir, args.output_file)
    else:
        output_path = os.path.join(args.outdir, f"{effective_channel_name}_quant.csv")

    # Run quantification (CPU mode)
    # Note: GPU mode removed - use GPU container if GPU acceleration needed
    logger.info('Running CPU quantification')
    run_quantification(
        mask_path=args.mask_file,
        channel_path=args.channel_tiff,
        output_path=output_path,
        min_area=args.min_area,
        channel_name=effective_channel_name
    )

    return 0


if __name__ == '__main__':
    exit(main())