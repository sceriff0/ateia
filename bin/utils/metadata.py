"""OME-TIFF and channel metadata utilities.

This module provides standardized functions for extracting and creating
metadata for microscopy images, particularly OME-TIFF format.

Examples
--------
>>> from metadata import extract_channel_names_from_ome
>>> channels = extract_channel_names_from_ome("registered.ome.tif")
>>> ['DAPI', 'SMA', 'panCK']

Notes
-----
This module consolidates metadata extraction logic used by register.py,
split_multichannel.py, convert_image.py, and segment_to_geojson.py.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

import tifffile

__all__ = [
    "extract_channel_names_from_ome",
    "is_nuclear",
    "pick_nuclear_index",
    "DEFAULT_NUCLEAR_MARKERS",
]

# Ordered preference for the nuclear/fiducial channel (the marker used to drive
# both cell segmentation and the registration fiducial). The first marker that
# matches any channel name wins. Mirrors params.nuclear_markers in nextflow.config.
#
# This tuple is the ONE permitted Python mirror of that default -- lib/MarkerUtils.groovy
# deliberately has none and throws instead, so nextflow.config stays the single place a
# parameter default lives (tests/test_no_duplicate_param_defaults.py). Every caller in
# the pipeline is handed the list explicitly; the fallback exists only so these helpers
# stay usable when a script is run by hand outside Nextflow.
DEFAULT_NUCLEAR_MARKERS = ("DAPI", "CELLTOX")


def _resolve_markers(nuclear_markers):
    """The configured markers, trimmed and upper-cased, or the default mirror.

    Each element is also split on commas and whitespace, mirroring
    ``lib/MarkerUtils.groovy``'s ``markerList``. A single ``"DAPI,CELLTOX"`` element is
    a marker that matches NO channel, and the failure is silent in both languages --
    every channel is classified non-nuclear, the nuclear channel is never dropped from
    moving slides, and the pre-computed group sizes are wrong with no error anywhere.
    """
    markers = [
        part.upper()
        for m in (nuclear_markers or ())
        for part in re.split(r"[,\s]+", str(m).strip())
        if part
    ]
    return markers or [str(m).upper() for m in DEFAULT_NUCLEAR_MARKERS]


def is_nuclear(channel_name, nuclear_markers=None):
    """True when ``channel_name`` names one of the configured nuclear markers.

    Case-insensitive SUBSTRING match, so ``DAPI_nuclear`` counts as nuclear. This is
    the same rule as ``lib/MarkerUtils.groovy``'s ``isNuclear``, and the two MUST stay
    in agreement: Groovy uses it to pre-compute how many single-channel TIFFs
    ``SPLIT_CHANNELS`` will emit for a patient (``CsvUtils.countChannelsPerPatient``),
    while Python uses it to decide which channels ``split_multichannel.py`` actually
    writes. A disagreement is an over- or under-count of a ``groupTuple`` size, not a
    crash -- see ``tests/unit/test_nuclear_markers.py``.

    Parameters
    ----------
    channel_name : str
        A single channel name, from metadata (never a filename).
    nuclear_markers : sequence of str, optional
        Marker names. Defaults to ``DEFAULT_NUCLEAR_MARKERS``.

    Returns
    -------
    bool
    """
    name = str(channel_name or "").strip().upper()
    if not name:
        return False
    return any(marker in name for marker in _resolve_markers(nuclear_markers))


def pick_nuclear_index(channel_names, nuclear_markers=None):
    """Index of the nuclear/fiducial channel, resolved by marker name.

    Channel identity comes from metadata (OME ``Channel:Name`` or the declared
    channel names), never from the filename. Markers are tried in order; the first
    marker that matches any channel (case-insensitive substring) wins, and that
    channel's index is returned.

    Parameters
    ----------
    channel_names : list of str
        Channel names in image/channel order.
    nuclear_markers : sequence of str, optional
        Ordered preference list of nuclear marker names. Defaults to
        ``DEFAULT_NUCLEAR_MARKERS`` (``('DAPI', 'CELLTOX')``).

    Returns
    -------
    int or None
        Index of the first matching channel, or ``None`` if no marker matches.
    """
    names = [str(n).upper() for n in (channel_names or [])]
    for marker in _resolve_markers(nuclear_markers):
        for i, name in enumerate(names):
            if marker in name:
                return i
    return None


def extract_channel_names_from_ome(filepath: str | Path) -> List[str]:
    """Extract channel names from OME-TIFF metadata.

    Parameters
    ----------
    filepath : str or Path
        Path to OME-TIFF file.

    Returns
    -------
    List[str]
        List of channel names from OME-XML metadata.
        Returns empty list if:
        - File is not OME-TIFF
        - OME metadata is missing/malformed
        - No channel information found

    Notes
    -----
    This function parses the OME-XML metadata block embedded in TIFF files.
    It handles both the standard OME namespace and files without proper namespace
    declarations.

    The OME-XML specification is defined at:
    https://www.openmicroscopy.org/Schemas/OME/2016-06

    Examples
    --------
    >>> extract_channel_names_from_ome("registered.ome.tif")
    ['DAPI', 'SMA', 'panCK', 'CD3']

    >>> extract_channel_names_from_ome("not_ome.tif")
    []
    """
    try:
        with tifffile.TiffFile(str(filepath)) as tif:
            if not hasattr(tif, "ome_metadata") or not tif.ome_metadata:
                return []

            # Parse XML
            root = ET.fromstring(tif.ome_metadata)

            # Define OME namespace
            ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}

            # Try with namespace first
            channels = root.findall(".//ome:Channel", ns)

            if not channels:
                # Fallback: try without namespace (some files may not use it properly)
                channels = root.findall(".//{*}Channel")

            # Extract channel names
            names = []
            for ch in channels:
                # Try 'Name' attribute first, fallback to 'ID'
                name = ch.get("Name") or ch.get("ID", f"Channel_{len(names)}")
                names.append(name)

            return names

    except Exception as e:
        logging.getLogger(__name__).debug(f"OME metadata parsing failed: {e}")
        return []
