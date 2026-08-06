#!/usr/bin/env python3
"""
Split multichannel TIFF images into individual single-channel TIFF files.
The nuclear/fiducial channel is only saved from the reference image.

Which channel counts as nuclear comes from ``--nuclear-markers`` (SPLIT_CHANNELS passes
``params.nuclear_markers``), resolved by the shared ``utils.metadata.is_nuclear`` rule --
not by a hardcoded ``"DAPI"``. That matters beyond configurability: Nextflow pre-computes
each patient's ``groupTuple`` size from the SAME rule in ``lib/MarkerUtils.groovy``
(via ``CsvUtils.countChannelsPerPatient``), so if this script kept a channel Groovy
expected to be dropped the group would over-fill and a per-patient consumer would run
twice against one published path.

Usage:
    python split_multichannel.py input.ome.tiff output_folder --is-reference
    python split_multichannel.py input.ome.tiff output_folder  # skips the nuclear channel
    python split_multichannel.py in.tiff out --nuclear-markers CELLTOX
"""

from __future__ import annotations

import argparse
import logging
import os

# Add parent directory to path to import lib modules
import sys
from pathlib import Path

import tifffile

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from image_utils import ensure_dir, normalize_image_dimensions
from logger import configure_logging, get_logger
from metadata import extract_channel_names_from_ome, is_nuclear
from validation import clip_negative_values

logger = get_logger(__name__)

__all__ = ["main"]


def split_multichannel_tiff(
    input_path, output_dir, is_reference=False, channel_names=None, nuclear_markers=None
):
    """
    Split a multichannel TIFF into single-channel TIFFs.
    The nuclear channel is only saved if is_reference=True.

    Parameters
    ----------
    input_path : str
        Path to the input multichannel TIFF file.
    output_dir : str
        Directory to save the single-channel TIFFs.
    is_reference : bool
        If True, saves the nuclear channel. If False, skips it.
    channel_names : list of str, optional
        Names for each channel. If None, tries to read from OME metadata.
    nuclear_markers : sequence of str, optional
        Marker names that identify the nuclear/fiducial channel
        (``params.nuclear_markers``). Defaults to ``DEFAULT_NUCLEAR_MARKERS``.

    Returns
    -------
    list of str
        Paths to the saved files.
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
        logger.info("  Using default channel names")
    else:
        logger.info(f"  Channel names from metadata: {len(channel_names)} channels")

    # Validate channel count
    if len(channel_names) != n_channels:
        logger.warning(
            f"  Warning: {len(channel_names)} names vs {n_channels} channels"
        )
        if len(channel_names) < n_channels:
            channel_names.extend(
                [f"Channel_{i}" for i in range(len(channel_names), n_channels)]
            )
        else:
            channel_names = channel_names[:n_channels]

    # Create output directory
    ensure_dir(output_dir)

    # Save each channel (skip the nuclear channel if not reference)
    saved_paths = []
    skipped_count = 0

    for i, name in enumerate(channel_names):
        # The one nuclear-marker rule: case-insensitive SUBSTRING match against the
        # configured markers, shared with convert_image.py (which already moved this
        # channel to index 0) and mirrored by lib/MarkerUtils.groovy. An exact match
        # would miss names like 'DAPI_nuclear'; a hardcoded 'DAPI' would miss a
        # CELLTOX-configured run and keep a channel Groovy already counted as dropped.
        is_nuclear_channel = is_nuclear(name, nuclear_markers)

        # Skip the nuclear channel if this is not the reference image
        if is_nuclear_channel and not is_reference:
            logger.info(f"  Skipping: {name} (nuclear channel from non-reference)")
            skipped_count += 1
            continue

        # Clean the channel name for use as filename
        clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

        # Detect filename collisions from sanitization (e.g. "CD3-105" and "CD3_105")
        candidate_path = os.path.join(output_dir, f"{clean_name}.tiff")
        if os.path.exists(candidate_path):
            suffix = 2
            while os.path.exists(
                os.path.join(output_dir, f"{clean_name}_{suffix}.tiff")
            ):
                suffix += 1
            logger.warning(
                f"  Filename collision: '{name}' sanitized to '{clean_name}' which already exists, "
                f"using '{clean_name}_{suffix}' instead"
            )
            clean_name = f"{clean_name}_{suffix}"

        output_path = os.path.join(output_dir, f"{clean_name}.tiff")

        channel_data = data[i]
        tifffile.imwrite(output_path, channel_data, bigtiff=True, compression="zlib")

        saved_paths.append(output_path)
        status = " (nuclear channel from reference)" if is_nuclear_channel else ""
        logger.info(f"  Saved: {clean_name}.tiff{status}")

    logger.info(f"✓ Saved {len(saved_paths)} channels, skipped {skipped_count}")
    return saved_paths


def main():
    """CLI entry point: split a multichannel TIFF into single-channel TIFFs."""
    parser = argparse.ArgumentParser(
        description="Split multichannel TIFF into single-channel TIFFs "
        "(the nuclear channel is kept only for the reference image)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("input", type=str, help="Path to input multichannel TIFF file")

    parser.add_argument(
        "output_dir", type=str, help="Directory to save single-channel TIFFs"
    )

    parser.add_argument(
        "--is-reference",
        action="store_true",
        help="This is the reference image (save the nuclear channel)",
    )

    parser.add_argument(
        "--channels",
        nargs="+",
        default=None,
        help="Channel names (optional, will try to read from OME metadata)",
    )

    parser.add_argument(
        "--nuclear-markers",
        nargs="+",
        default=None,
        help="Marker names identifying the nuclear/fiducial channel, which is kept "
        "only on the reference image. SPLIT_CHANNELS always passes "
        "params.nuclear_markers; the default is only for standalone use.",
    )

    args = parser.parse_args()

    configure_logging(level=logging.INFO)

    logger.info("=" * 80)
    logger.info("SPLIT MULTICHANNEL TIFF")
    logger.info("=" * 80)

    split_multichannel_tiff(
        args.input,
        args.output_dir,
        args.is_reference,
        args.channels,
        args.nuclear_markers,
    )

    logger.info("=" * 80)
    logger.info("✓ COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
