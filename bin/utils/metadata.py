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
split_multichannel.py, and pad_image.py.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

import tifffile

__all__ = [
    "extract_channel_names_from_ome",
]


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
