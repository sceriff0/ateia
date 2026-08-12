"""The one pixel-size rule.

``params.pixel_size`` owns every micrometre conversion in this pipeline: the µm
measurements written into ``cells.geojson``, the ``PhysicalSize`` stamped on the
published pyramid, and InstantSeg's rescaling target all read it and nothing else.
That single ownership is deliberate -- it is the same "exactly one owner" rule the
resource labels and the nuclear-marker resolution follow -- but it has a failure mode.
An image whose own OME header claims a different scale is converted, registered,
segmented and quantified at the *configured* scale with nothing said about the
disagreement, and the symptom surfaces a long way from the cause: the sibling
``qupath-extension-flowpath`` sees a scale it cannot reconcile and warns there, in
QuPath, about a decision this pipeline made silently several steps earlier.

This module is the one place the two values are put side by side.
``read_ome_pixel_size`` reports what the file claims; ``warn_on_pixel_size_mismatch``
compares it against what was configured and says so, loudly, naming both numbers.

Neither function changes a number. The configured value stays authoritative, so a run's
outputs do not depend on whatever metadata happens to be embedded in its inputs -- which
in whole-slide imaging is frequently absent, wrong, or in units the writer guessed at.
The point is that the operator learns about it at ``CONVERT_IMAGE`` and can set
``--pixel_size`` correctly, rather than discovering it in QuPath after the fact.

Import convention: flat (``from pixel_size import ...``) by scripts that
``sys.path.insert(0, .../bin/utils)`` first, matching ``measurements.py`` and
``image_utils.py``.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

import tifffile

__all__ = ["read_ome_pixel_size", "warn_on_pixel_size_mismatch", "PIXEL_SIZE_RTOL"]

# Relative tolerance for calling two pixel sizes "the same". 1% is well below any real
# objective/scanner difference (the smallest step between adjacent magnifications is
# tens of percent) and well above float-formatting noise in an OME header, where a
# 0.325 µm scale is routinely serialised as 0.32499998807907104.
PIXEL_SIZE_RTOL = 0.01

# OME-XML records the unit alongside the value, and writers do use more than µm.
# A value read without its unit is not a pixel size, it is a number -- so an
# unrecognised unit yields None rather than a silently mis-scaled comparison.
_UNIT_TO_UM = {
    "µm": 1.0,
    "um": 1.0,
    "micrometer": 1.0,
    "micrometre": 1.0,
    "micron": 1.0,
    "microns": 1.0,
    "nm": 1e-3,
    "nanometer": 1e-3,
    "mm": 1e3,
    "millimeter": 1e3,
    "cm": 1e4,
    "m": 1e6,
}


def _to_um(raw: Optional[str], unit: Optional[str]) -> Optional[float]:
    """Convert one OME PhysicalSize attribute to µm, or None if it cannot be trusted."""
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # OME's default unit when the attribute is absent is µm, per the 2016-06 schema.
    factor = _UNIT_TO_UM.get((unit or "µm").strip())
    if factor is None:
        return None
    return value * factor


def read_ome_pixel_size(
    path: str | Path,
) -> Tuple[Optional[float], Optional[float]]:
    """Read ``PhysicalSizeX``/``PhysicalSizeY`` from a TIFF's OME header, in µm.

    Returns ``(None, None)`` for a file with no OME header, no ``Pixels`` element, no
    ``PhysicalSize`` attributes, or a unit this module does not recognise. Every one of
    those is ordinary rather than exceptional -- most intermediates in this pipeline
    carry no scale at all -- so none of them raises.

    Only OME metadata is read. The TIFF ``XResolution``/``ResolutionUnit`` tags are a
    different and much less reliable convention; ``bin/convert_image.py`` handles those
    for the vendor formats where they are the only thing available.
    """
    try:
        with tifffile.TiffFile(str(path)) as tif:
            ome_xml = getattr(tif, "ome_metadata", None)
            if not ome_xml or not isinstance(ome_xml, str):
                return (None, None)
            root = ET.fromstring(ome_xml)
    except Exception:
        # An unreadable or malformed header means "no scale available", which is
        # exactly the (None, None) case. Reading metadata must never fail a run.
        return (None, None)

    pixels = root.find(".//{*}Pixels")
    if pixels is None:
        return (None, None)

    return (
        _to_um(pixels.get("PhysicalSizeX"), pixels.get("PhysicalSizeXUnit")),
        _to_um(pixels.get("PhysicalSizeY"), pixels.get("PhysicalSizeYUnit")),
    )


def warn_on_pixel_size_mismatch(
    detected: Tuple[Optional[float], Optional[float]] | Optional[float],
    configured: float,
    *,
    source: str,
    logger: logging.Logger,
    rtol: float = PIXEL_SIZE_RTOL,
) -> bool:
    """Compare an image's own scale against the configured one; warn if they disagree.

    Returns True when a mismatch was reported, so a caller can count them. The
    configured value is authoritative either way -- this function never returns a scale
    to use, precisely so that no caller can accidentally start preferring the metadata.

    An image with no embedded scale is not a mismatch. It is the normal case for the
    pipeline's own intermediates and says nothing about whether ``--pixel_size`` is
    right, so warning about it would train operators to ignore the warning that matters.
    """
    if not isinstance(detected, tuple):
        detected = (detected, detected)
    det_x, det_y = detected

    reported = False
    for axis, det in (("X", det_x), ("Y", det_y)):
        if det is None:
            continue
        if abs(det - configured) <= rtol * abs(configured):
            continue
        logger.warning(
            "  [SCALE MISMATCH] %s reports PhysicalSize%s = %g µm/px, but the run is "
            "configured with --pixel_size %g (%.0f%% apart). Every µm measurement -- "
            "GeoJSON centroids and areas, the published pyramid's PhysicalSize, "
            "InstantSeg's rescaling -- uses the configured %g. If the image is right, "
            "re-run with --pixel_size %g.",
            source,
            axis,
            det,
            configured,
            100.0 * abs(det - configured) / abs(configured),
            configured,
            det,
        )
        reported = True

    if not reported and det_x is None and det_y is None:
        logger.debug(
            "  %s carries no OME pixel size; using the configured %g µm/px.",
            source,
            configured,
        )
    return reported
