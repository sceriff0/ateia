#!/usr/bin/env python3
"""
Split multichannel TIFF images into individual single-channel TIFF files.
The nuclear/fiducial channel is only saved from the reference image.

A marker has TWO names and they must not be confused: the DECLARED name (the
samplesheet's spelling, e.g. ``HLA.DR``) is the marker's IDENTITY and is what fills the
``<marker>`` slot of the downstream measurement key, while the FILE STEM (``HLA_DR``) is
the sanitised form and names files only. This script writes stems and never reports one
back as an identity; ``lib/ChannelName.groovy`` owns the mapping and passes it in with
``--file-stems`` so the Nextflow ``stub:`` path cannot name a channel differently.

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

from channel_name import file_stems as compute_file_stems
from image_utils import ensure_dir, normalize_image_dimensions
from logger import configure_logging, get_logger
from metadata import extract_channel_names_from_ome, is_nuclear
from pixel_size import read_ome_pixel_size, warn_on_pixel_size_mismatch
from validation import clip_negative_values

logger = get_logger(__name__)

__all__ = ["main"]


def split_multichannel_tiff(
    input_path,
    output_dir,
    is_reference=False,
    channel_names=None,
    nuclear_markers=None,
    pixel_size=0.325,
    file_stems=None,
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
    file_stems : list of str, optional
        Filesystem-safe stems, ONE PER ENTRY OF ``channel_names``, already
        computed by ``lib/ChannelName.groovy`` (SPLIT_CHANNELS passes
        ``--file-stems``). Supplying them is what makes that process's
        ``script:`` and ``stub:`` paths write the same filename for a channel
        whose declared name needs sanitising. When absent -- standalone use, or
        names read from OME metadata -- the same rule is applied here via
        ``bin/utils/channel_name.py``.

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

    # The declared name is the marker's IDENTITY (it fills the <marker> slot of the
    # FlowPath measurement key); the stem only names a file. Resolve the mapping ONCE,
    # over the full declared list, so a collision's disambiguation suffix does not
    # depend on which channels this particular slide happens to write.
    if file_stems is not None and len(file_stems) == len(channel_names):
        stems = list(file_stems)
    else:
        if file_stems is not None:
            logger.warning(
                f"  --file-stems has {len(file_stems)} entries for {len(channel_names)} "
                "channels; recomputing locally rather than pairing them up wrong"
            )
        stems = compute_file_stems(channel_names)

    # Create output directory
    ensure_dir(output_dir)

    # These single-channel TIFFs used to be written with no scale at all -- a bare
    # imwrite, no resolution tags, no OME header -- so anything that opened one directly
    # saw 1 px = 1 unit, and MERGE_AND_PYRAMID had nothing to read even in principle.
    # They now carry the configured scale as standard TIFF resolution tags. Standard
    # tags rather than an OME header on purpose: an OME header here would be a second,
    # channel-less source of channel names for extract_channel_names_from_ome to find.
    detected = read_ome_pixel_size(input_path)
    warn_on_pixel_size_mismatch(
        detected, pixel_size, source=Path(input_path).name, logger=logger
    )
    # CENTIMETER, so resolution is pixels-per-cm: 1e4 µm/cm divided by µm/px.
    resolution = (1e4 / pixel_size, 1e4 / pixel_size)

    # Save each channel (skip the nuclear channel if not reference)
    saved_paths = []
    skipped_count = 0

    for i, (name, clean_name) in enumerate(zip(channel_names, stems)):
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

        # `clean_name` is the FILE STEM for this channel, already disambiguated
        # against every other declared channel by `compute_file_stems`. It is not
        # re-derived here and it is never fed back into the marker's identity:
        # the declared `name` is what reaches the measurement key.
        if clean_name != name:
            logger.info(f"  '{name}' -> {clean_name}.tiff (filename only)")

        output_path = os.path.join(output_dir, f"{clean_name}.tiff")

        channel_data = data[i]
        tifffile.imwrite(
            output_path,
            channel_data,
            bigtiff=True,
            compression="zlib",
            resolution=resolution,
            resolutionunit="CENTIMETER",
        )

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

    parser.add_argument(
        "--file-stems",
        nargs="+",
        default=None,
        help="Filesystem-safe stems, one per --channels entry, computed by "
        "lib/ChannelName.groovy. SPLIT_CHANNELS always passes these so its script: "
        "and stub: paths write identical filenames; omit them and the same rule is "
        "applied locally.",
    )

    parser.add_argument(
        "--pixel-size",
        dest="pixel_size",
        type=float,
        default=0.325,
        help="Configured scale in µm/px (SPLIT_CHANNELS passes params.pixel_size). "
        "Stamped on each single-channel TIFF as resolution tags, and the value the "
        "input's own scale is checked against.",
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
        args.pixel_size,
        args.file_stems,
    )

    logger.info("=" * 80)
    logger.info("✓ COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
