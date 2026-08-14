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

__all__ = [
    "read_ome_pixel_size",
    "warn_on_pixel_size_mismatch",
    "PIXEL_SIZE_RTOL",
    "Pixels",
    "Microns",
    "Unrecorded",
    "Measurement",
    "UnitError",
    "to_microns",
]


# ---------------------------------------------------------------------------
# A measurement carries its unit
# ---------------------------------------------------------------------------
# The µm conversion above is the pipeline's ONE rule, but a rule can only be applied
# where somebody notices it is needed. `bin/generate_qc_report.py` assigned STARE's
# `coarse_tre_px` -- a pixel count -- straight into a field called `feature_tre_um` and
# rendered it under a "Feature-TRE (µm)" header, with no conversion anywhere on the
# path, because the only thing distinguishing the two was a suffix in a dict key. At
# the shipped 0.325 µm/px that mistake is a 1/0.325 = 3.08x error: numerically
# indistinguishable from the 3.0x threshold the same table uses to flag bad
# registration.
#
# These three types make the unit part of the value, so the mismatch is a TypeError at
# the seam rather than a plausible number in a report. `Unrecorded` is not a hole in
# the scheme -- it is the honest label for a producer that never stated a unit (VALIS's
# `error_df` D columns, copied through `bin/register.py` unmodified), and it converts
# to nothing at all.


class UnitError(TypeError):
    """A conversion to µm that cannot be justified -- an expected STATE, not a bug.

    Dedicated so that ``bin/generate_qc_report.py`` can catch "this value cannot be
    expressed in µm" and render a reason, WITHOUT that same ``except`` also swallowing a
    genuine coding error inside the conversion and rendering it as a plausible-looking
    QC cell. A swallowed error that looks like normal output is the worst shape a bug
    can take in a QC report.

    Still a ``TypeError``: the brief's rule is that a px value reaching a µm field is a
    *type* error rather than a naming convention, and subclassing keeps that true for
    any caller that catches the broad type.
    """


class Measurement:
    """A number that knows what it is measured in.

    Deliberately a plain class rather than a ``dataclass``: several test modules load
    this file through ``importlib.util.spec_from_file_location`` without registering it
    in ``sys.modules``, and ``dataclasses`` resolves field types by looking the module
    up there -- which raises before a single assertion runs. A unit type that only works
    under one import style is not a unit type.
    """

    __slots__ = ("value",)
    unit = "unrecorded"

    def __init__(self, value: float) -> None:
        # Abstract: the base is the isinstance root and the type alias, never a value.
        # An instantiable base is a fourth, unit-less measurement type hiding inside the
        # vocabulary -- and `to_microns` would tell it it was "a bare number, not a
        # measurement", which is false about the object it was just handed.
        if type(self) is Measurement:
            raise TypeError(
                "Measurement is abstract; use Pixels(...), Microns(...) or "
                "Unrecorded(...) so the value carries a unit"
            )
        self.value = float(value)

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.value == other.value

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.value))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.value!r})"

    def __str__(self) -> str:
        return f"{self.value:.3f} {self.unit}"


class Pixels(Measurement):
    """A distance in image pixels."""

    __slots__ = ()
    unit = "px"


class Microns(Measurement):
    """A distance in micrometres."""

    __slots__ = ()
    unit = "µm"


class Unrecorded(Measurement):
    """A number whose producer never stated a unit; convertible to nothing."""

    __slots__ = ()
    unit = "unrecorded"


def to_microns(measurement: Measurement, pixel_size_um: Optional[float]) -> Microns:
    """Convert a measurement to micrometres using the CONFIGURED scale.

    A conversion, not a detection: the scale arrives as an argument and nothing here
    reads an image, so this cannot become a second owner of the pixel size the way a
    "resolve the scale for me" helper would. That is the same property
    ``warn_on_pixel_size_mismatch`` protects by never returning a scale.

    Raises ``UnitError`` -- one exception for every way the conversion cannot be
    justified -- when the argument is a bare number rather than a measurement, when its
    unit was never recorded, or when no usable scale was supplied. Refusing is the
    point: every one of those cases used to render as a confident µm figure. Anything
    else raised from here is a bug and is left to propagate.
    """
    if isinstance(measurement, Microns):
        return measurement
    if isinstance(measurement, Unrecorded):
        raise UnitError(
            f"cannot express {measurement.value!r} in µm: its producer recorded no "
            "unit, so no scale can convert it"
        )
    if not isinstance(measurement, Pixels):
        raise UnitError(
            f"{measurement!r} is a bare number, not a measurement; wrap it in "
            "Pixels(...) or Microns(...) at the point it is read so the unit travels "
            "with the value"
        )
    if not pixel_size_um:
        raise UnitError(
            f"cannot express {measurement.value!r} px in µm: no pixel size was "
            "supplied (params.pixel_size is the one owner of that scale)"
        )
    return Microns(measurement.value * float(pixel_size_um))


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
