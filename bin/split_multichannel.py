#!/usr/bin/env python3
"""
Split multichannel TIFF images into individual single-channel TIFF files.
DAPI channel is only saved from the reference image.

Usage:
    python split_multichannel.py input.ome.tiff output_folder --is-reference
    python split_multichannel.py input.ome.tiff output_folder  # skips DAPI
"""
from __future__ import annotations

import argparse
import os
import tifffile
import numpy as np
import logging

# Add parent directory to path to import lib modules
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

from logger import get_logger, configure_logging
from image_utils import normalize_image_dimensions, ensure_dir
from metadata import extract_channel_names_from_ome
from validation import clip_negative_values

logger = get_logger(__name__)

__all__ = ["main"]


def split_multichannel_tiff(input_path, output_dir, is_reference=False, channel_names=None):
    """
    Split a multichannel TIFF into single-channel TIFFs.
    DAPI is only saved if is_reference=True.

    Parameters:
    -----------
    input_path : str
        Path to the input multichannel TIFF file
    output_dir : str
        Directory to save the single-channel TIFFs
    is_reference : bool
        If True, saves DAPI channel. If False, skips DAPI.
    channel_names : list of str, optional
        Names for each channel. If None, tries to read from OME metadata.

    Returns:
    --------
    list : Paths to the saved files
    """
    # Read the image
    logger.info(f"Reading: {input_path}")
    logger.info(f"  Is reference: {is_reference}")

    img = tifffile.imread(input_path)
    logger.info(f"  Image shape: {img.shape}, dtype: {img.dtype}")

    # Clip negative values (VALIS warping artifact)
    img = clip_negative_values(img, logger, stage_name="split_multichannel")

    # Normalize to (C, H, W) format
    data = normalize_image_dimensions(img)
    n_channels = data.shape[0]
    logger.info(f"  Normalized to (C, H, W) with {n_channels} channels")

    # Get channel names
    if channel_names is None:
        channel_names = extract_channel_names_from_ome(input_path) or None

    if channel_names is None:
        channel_names = [f"Channel_{i}" for i in range(n_channels)]
        logger.info(f"  Using default channel names")
    else:
        logger.info(f"  Channel names from metadata: {len(channel_names)} channels")

    # Validate channel count
    if len(channel_names) != n_channels:
        logger.warning(f"  Warning: {len(channel_names)} names vs {n_channels} channels")
        if len(channel_names) < n_channels:
            channel_names.extend([f"Channel_{i}" for i in range(len(channel_names), n_channels)])
        else:
            channel_names = channel_names[:n_channels]

    # Create output directory
    ensure_dir(output_dir)

    # Save each channel (skip DAPI if not reference)
    saved_paths = []
    skipped_count = 0

    for i, name in enumerate(channel_names):
        # Check if this is DAPI channel. Use substring match to stay consistent
        # with convert_image.py's DAPI detection ('DAPI' in ch.upper()) — an
        # exact match here would miss names like 'DAPI_nuclear' that convert_image
        # already moved to channel 0, causing the DAPI-equivalent to be wrongly
        # saved for non-reference images.
        is_dapi = 'DAPI' in name.upper()

        # Skip DAPI if this is not the reference image
        if is_dapi and not is_reference:
            logger.info(f"  Skipping: {name} (DAPI from non-reference)")
            skipped_count += 1
            continue

        # Clean the channel name for use as filename
        clean_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in name)

        # Detect filename collisions from sanitization (e.g. "CD3-105" and "CD3_105")
        candidate_path = os.path.join(output_dir, f"{clean_name}.tiff")
        if os.path.exists(candidate_path):
            suffix = 2
            while os.path.exists(os.path.join(output_dir, f"{clean_name}_{suffix}.tiff")):
                suffix += 1
            logger.warning(f"  Filename collision: '{name}' sanitized to '{clean_name}' which already exists, "
                           f"using '{clean_name}_{suffix}' instead")
            clean_name = f"{clean_name}_{suffix}"

        output_path = os.path.join(output_dir, f"{clean_name}.tiff")

        channel_data = data[i]
        tifffile.imwrite(output_path, channel_data, bigtiff=True, compression='zlib')

        saved_paths.append(output_path)
        status = " (DAPI from reference)" if is_dapi else ""
        logger.info(f"  Saved: {clean_name}.tiff{status}")

    logger.info(f"✓ Saved {len(saved_paths)} channels, skipped {skipped_count}")
    return saved_paths


def main():
    parser = argparse.ArgumentParser(
        description='Split multichannel TIFF into single-channel TIFFs (DAPI only from reference)',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        'input',
        type=str,
        help='Path to input multichannel TIFF file'
    )

    parser.add_argument(
        'output_dir',
        type=str,
        help='Directory to save single-channel TIFFs'
    )

    parser.add_argument(
        '--is-reference',
        action='store_true',
        help='This is the reference image (save DAPI)'
    )

    parser.add_argument(
        '--channels',
        nargs='+',
        default=None,
        help='Channel names (optional, will try to read from OME metadata)'
    )

    args = parser.parse_args()

    configure_logging(level=logging.INFO)

    logger.info("=" * 80)
    logger.info("SPLIT MULTICHANNEL TIFF")
    logger.info("=" * 80)

    split_multichannel_tiff(args.input, args.output_dir, args.is_reference, args.channels)

    logger.info("=" * 80)
    logger.info("✓ COMPLETE")
    logger.info("=" * 80)


if __name__ == '__main__':
    main()
