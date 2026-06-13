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
    "compute_compartment_intensities",
    "quantify_single_channel",
    "run_quantification",
]

# Per-compartment quantification: compartment names in the order they are emitted.
# These become part of the QuPath measurement key "<marker>: <Compartment>: <Stat>".
COMPARTMENT_NAMES = ("Nucleus", "Cytoplasm", "Cell")


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


def _safe_mean(sums: NDArray, counts: NDArray) -> NDArray:
    """Element-wise sums / counts with 0.0 where counts is 0."""
    with np.errstate(divide='ignore', invalid='ignore'):
        means = np.divide(sums, counts)
    return np.nan_to_num(means, nan=0.0)


def compute_compartment_intensities(
    cell_mask: NDArray,
    nuclei_mask: NDArray,
    channel: NDArray,
    channel_name: str,
    expanded: bool = False,
) -> pd.DataFrame:
    """Compute per-compartment signal (Nucleus / Cytoplasm / Cell) for one channel.

    The nuclear signal of cell ``C`` is measured over the pixels that belong to
    cell ``C`` **and** to any nucleus, i.e. ``(cell_mask == C) & (nuclei_mask > 0)``.
    This keys nuclear statistics by the *cell* label directly, so no nucleus->cell
    label pairing is required and the two masks need not share label IDs. Cytoplasm
    is derived as ``Cell - Nucleus`` by subtraction (guaranteed non-negative because
    the measured nuclear region is a subset of the cell). Works for any backend that
    exposes both masks (StarDist, CellSAM, InstanSeg, ...).

    Output columns are the *final QuPath measurement keys*, so they flow unchanged
    through merge_quant_csvs -> export_geojson -> GeoJSON:

    - ``label``
    - ``<channel>``  (== whole-cell mean, kept for backward compatibility)
    - ``<channel>: Nucleus: Mean`` / ``: Cytoplasm: Mean`` / ``: Cell: Mean``
    - with ``expanded=True``, additionally ``: <Compartment>: Median`` and ``: Sum``.

    Parameters
    ----------
    cell_mask, nuclei_mask : ndarray, shape (Y, X)
        Whole-cell and nuclear instance masks (background = 0).
    channel : ndarray, shape (Y, X)
        Single-channel intensity image.
    channel_name : str
        Marker name used to build measurement keys.
    expanded : bool
        If True, also emit Median and Sum (integrated density) per compartment.

    Returns
    -------
    DataFrame
        One row per (whole-cell) label. Empty DataFrame if no cells.
    """
    cell_mask = np.ascontiguousarray(cell_mask.squeeze())
    nuclei_mask = np.ascontiguousarray(nuclei_mask.squeeze())
    channel = channel.squeeze()

    if cell_mask.shape != nuclei_mask.shape:
        raise ValueError(
            f"cell/nuclei mask shape mismatch: {cell_mask.shape} vs {nuclei_mask.shape}"
        )
    if cell_mask.shape != channel.shape:
        raise ValueError(
            f"mask/channel shape mismatch: {cell_mask.shape} vs {channel.shape}"
        )

    valid_labels = np.unique(cell_mask)
    valid_labels = valid_labels[valid_labels != 0]
    if len(valid_labels) == 0:
        logger.warning("[WARN] No cells found in cell mask")
        return pd.DataFrame()

    n = int(cell_mask.max()) + 1
    flat_cell = cell_mask.ravel()
    flat_val = channel.ravel().astype(np.float64)
    nuc_fg = nuclei_mask.ravel() > 0

    # Whole-cell sums/counts keyed by cell label.
    cell_sum = np.bincount(flat_cell, weights=flat_val, minlength=n)
    cell_count = np.bincount(flat_cell, minlength=n)

    # Nuclear region (cell ∩ nucleus) keyed by *cell* label — label-pairing free.
    nuc_cell_labels = np.where(nuc_fg, flat_cell, 0)
    nuc_sum = np.bincount(nuc_cell_labels, weights=flat_val, minlength=n)
    nuc_count = np.bincount(nuc_cell_labels, minlength=n)

    # Cytoplasm = whole-cell minus nuclear region (per cell, by subtraction).
    cyto_sum = cell_sum - nuc_sum
    cyto_count = cell_count - nuc_count

    sums = {"Nucleus": nuc_sum, "Cytoplasm": cyto_sum, "Cell": cell_sum}
    counts = {"Nucleus": nuc_count, "Cytoplasm": cyto_count, "Cell": cell_count}

    out: dict = {"label": valid_labels}
    # Backward-compatible bare marker column (== whole-cell mean).
    out[channel_name] = _safe_mean(cell_sum, cell_count)[valid_labels]
    for comp in COMPARTMENT_NAMES:
        out[f"{channel_name}: {comp}: Mean"] = _safe_mean(sums[comp], counts[comp])[valid_labels]

    if expanded:
        # Sum (integrated density) is already computed above.
        for comp in COMPARTMENT_NAMES:
            out[f"{channel_name}: {comp}: Sum"] = sums[comp][valid_labels]
        # Median needs a per-pixel gather; restrict to foreground cell pixels only.
        fg = flat_cell != 0
        df_px = pd.DataFrame({
            "label": flat_cell[fg],
            "val": flat_val[fg],
            "nuc": nuc_fg[fg],
        })
        medians = {
            "Cell": df_px.groupby("label")["val"].median(),
            "Nucleus": df_px[df_px["nuc"]].groupby("label")["val"].median(),
            "Cytoplasm": df_px[~df_px["nuc"]].groupby("label")["val"].median(),
        }
        for comp in COMPARTMENT_NAMES:
            series = medians[comp].reindex(valid_labels).fillna(0.0)
            out[f"{channel_name}: {comp}: Median"] = series.values

    return pd.DataFrame(out)


def quantify_single_channel(
    mask: NDArray,
    channel_image: NDArray,
    channel_name: str,
    nuclei_mask: Optional[NDArray] = None,
    expanded: bool = False,
) -> pd.DataFrame:
    """Quantify a single channel (intensity only).

    Morphology is computed separately by EXTRACT_CELL_PROPERTIES.

    When ``nuclei_mask`` is provided, per-compartment intensities (Nucleus /
    Cytoplasm / Cell) are computed via :func:`compute_compartment_intensities`.
    Otherwise the legacy whole-cell mean is computed.

    Parameters
    ----------
    mask : ndarray, shape (Y, X)
        Whole-cell segmentation mask.
    channel_image : ndarray, shape (Y, X)
        Channel intensity image.
    channel_name : str
        Name of the channel/marker.
    nuclei_mask : ndarray, optional
        Nuclear segmentation mask. If given, enables per-compartment quantification.
    expanded : bool
        With ``nuclei_mask``, also emit Median and Sum per compartment.

    Returns
    -------
    DataFrame
        Intensity per cell. Columns: ``label`` plus either ``{channel_name}``
        (legacy) or the per-compartment measurement keys.
    """
    mask = np.ascontiguousarray(mask.squeeze())

    if nuclei_mask is not None:
        return compute_compartment_intensities(
            mask, nuclei_mask, channel_image, channel_name, expanded=expanded
        )

    labels = np.unique(mask)
    valid_labels = labels[labels != 0]

    if len(valid_labels) == 0:
        logger.warning("[WARN] No cells found in mask")
        return pd.DataFrame()

    # Compute intensity for this channel (no morphology — that's done once upstream)
    intensity_series = compute_channel_intensity(
        mask, channel_image, valid_labels, channel_name
    )

    result_df = pd.DataFrame({
        'label': valid_labels,
        channel_name: intensity_series.values,
    })

    return result_df


def _load_mask(mask_path: str) -> NDArray:
    """Load a segmentation mask from .npy or .tif, squeezed to 2D."""
    if mask_path.endswith('.npy'):
        return np.load(mask_path).squeeze()
    loaded, _ = load_image(mask_path)
    return loaded.squeeze()


def run_quantification(
    mask_path: str,
    channel_path: str,
    output_path: str,
    channel_name: str = None,
    nuclei_mask_path: Optional[str] = None,
    expanded: bool = False,
) -> pd.DataFrame:
    """Run quantification for a single channel.

    Parameters
    ----------
    mask_path : str
        Path to whole-cell segmentation mask (.npy or .tif file).
    channel_path : str
        Path to channel image file (.tif).
    output_path : str
        Path to save output CSV file.
    channel_name : str, optional
        Explicit channel/marker name. If not provided, will be parsed
        from the channel file basename.
    nuclei_mask_path : str, optional
        Path to the nuclear mask. If provided, per-compartment quantification
        (Nucleus / Cytoplasm / Cell) is performed instead of whole-cell only.
    expanded : bool
        With ``nuclei_mask_path``, also emit Median and Sum per compartment.

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

    # Optionally load the nuclear mask for per-compartment quantification
    nuclei_mask = None
    if nuclei_mask_path:
        logger.info(f"Loading nuclei mask: {nuclei_mask_path}")
        try:
            nuclei_mask = _load_mask(nuclei_mask_path)
        except FileNotFoundError:
            logger.error(f"[FAIL] Nuclei mask not found: {nuclei_mask_path}")
            raise FileNotFoundError(f"Nuclei mask not found: {nuclei_mask_path}")
        except Exception as e:
            logger.error(f"[FAIL] Failed to load nuclei mask from {nuclei_mask_path}: {e}")
            raise ValueError(f"Failed to load nuclei mask from {nuclei_mask_path}: {e}") from e
        mode = "expanded (Mean/Median/Sum)" if expanded else "standard (Mean)"
        logger.info(f"Per-compartment quantification enabled: Nucleus/Cytoplasm/Cell, {mode}")

    # Quantify
    result_df = quantify_single_channel(
        mask, channel_image, channel_name,
        nuclei_mask=nuclei_mask, expanded=expanded,
    )

    # Save
    if not result_df.empty:
        result_df.to_csv(output_path, index=False)
        logger.info(f"[OK] Saved {len(result_df)} cells to {output_path}")
    else:
        logger.warning("[WARN] No results to save")
        # Empty CSV with the SAME columns a non-empty result would have, so the
        # downstream per-channel merge stays consistent when a channel's mask
        # contains no cells (compartment mode would otherwise emit only 2 cols).
        cols = ['label', channel_name]
        if nuclei_mask is not None:
            cols += [f"{channel_name}: {c}: Mean" for c in COMPARTMENT_NAMES]
            if expanded:
                cols += [f"{channel_name}: {c}: Sum" for c in COMPARTMENT_NAMES]
                cols += [f"{channel_name}: {c}: Median" for c in COMPARTMENT_NAMES]
        empty_df = pd.DataFrame(columns=cols)
        empty_df.to_csv(output_path, index=False)

    logger.info("Quantification complete")

    return result_df


def run_quantification_gpu(
    mask_path: str,
    channel_path: str,
    output_path: str,
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

    # Get all non-background labels
    valid_labels = cp.unique(mask_gpu)
    valid_labels = valid_labels[valid_labels != 0]

    if len(valid_labels) == 0:
        logger.warning("No cells found in mask")
        empty_df = pd.DataFrame(columns=['label', channel_name])
        empty_df.to_csv(output_path, index=False)
        return empty_df

    # Compute intensity only (morphology is done by EXTRACT_CELL_PROPERTIES)
    logger.info("Computing intensity (GPU)...")
    flat_mask = mask_gpu.ravel()
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
    del mask_gpu, channel_gpu, flat_mask, flat_channel
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
        help='Path to whole-cell segmentation mask (.npy or .tif)'
    )
    parser.add_argument(
        '--nuclei_mask_file',
        type=str,
        default=None,
        help='Path to nuclear mask (.npy or .tif). If given, enables per-compartment '
             'quantification (Nucleus/Cytoplasm/Cell).'
    )
    parser.add_argument(
        '--expanded',
        action='store_true',
        help='With --nuclei_mask_file, also emit Median and Sum per compartment '
             '(default: Mean only).'
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
        channel_name=effective_channel_name,
        nuclei_mask_path=args.nuclei_mask_file,
        expanded=args.expanded,
    )

    return 0


if __name__ == '__main__':
    exit(main())