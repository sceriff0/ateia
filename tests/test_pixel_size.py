"""Tests for bin/utils/pixel_size.py — the one pixel-size rule.

`params.pixel_size` owns every µm conversion in the pipeline. These tests pin the two
halves of that ownership: reading what an image *claims* its scale is, and saying so
when it disagrees with what the run was configured with. Neither half may change a
number — the warning exists precisely so that the configured value can stay
authoritative without being silent about it.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import numpy as np
import pytest
import tifffile

_SPEC = importlib.util.spec_from_file_location(
    "pixel_size", Path(__file__).resolve().parent.parent / "bin" / "utils" / "pixel_size.py"
)
ps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ps)


def _write_ome(path: Path, **kwargs) -> Path:
    tifffile.imwrite(
        path, np.zeros((4, 4), dtype=np.uint16), metadata={"axes": "YX", **kwargs}
    )
    return path


# ── read_ome_pixel_size ────────────────────────────────────────────────────────
def test_reads_physical_size_in_um(tmp_path):
    p = _write_ome(tmp_path / "a.ome.tif", PhysicalSizeX=0.2125, PhysicalSizeY=0.2125)
    assert ps.read_ome_pixel_size(p) == pytest.approx((0.2125, 0.2125), rel=1e-6)


def test_unit_is_honoured_not_assumed(tmp_path):
    """A value without its unit is a number, not a scale.

    OME writers do use nm; reading 212.5 nm as 212.5 µm would be a 1000x error that
    reads as a plausible pixel size for a downsampled overview, so it would not look
    obviously wrong anywhere downstream.
    """
    p = _write_ome(
        tmp_path / "nm.ome.tif",
        PhysicalSizeX=212.5,
        PhysicalSizeXUnit="nm",
        PhysicalSizeY=212.5,
        PhysicalSizeYUnit="nm",
    )
    assert ps.read_ome_pixel_size(p) == pytest.approx((0.2125, 0.2125), rel=1e-6)


def test_unrecognised_unit_reads_as_absent(tmp_path):
    p = _write_ome(
        tmp_path / "weird.ome.tif", PhysicalSizeX=1.0, PhysicalSizeXUnit="parsec"
    )
    assert ps.read_ome_pixel_size(p)[0] is None


def test_no_ome_header_is_not_an_error(tmp_path):
    """The normal case for this pipeline's own intermediates."""
    p = tmp_path / "plain.tiff"
    tifffile.imwrite(p, np.zeros((4, 4), dtype=np.uint16))
    assert ps.read_ome_pixel_size(p) == (None, None)


def test_unreadable_file_is_not_an_error(tmp_path):
    p = tmp_path / "not-a-tiff.tiff"
    p.write_bytes(b"definitely not a tiff")
    assert ps.read_ome_pixel_size(p) == (None, None)


def test_nonsense_values_read_as_absent(tmp_path):
    p = _write_ome(tmp_path / "zero.ome.tif", PhysicalSizeX=0.0)
    assert ps.read_ome_pixel_size(p)[0] is None


# ── warn_on_pixel_size_mismatch ────────────────────────────────────────────────
def test_disagreement_warns_and_names_both_numbers(caplog):
    with caplog.at_level(logging.WARNING):
        fired = ps.warn_on_pixel_size_mismatch(
            (0.2125, 0.2125), 0.325, source="P001.ome.tiff", logger=logging.getLogger()
        )
    assert fired
    text = caplog.text
    assert "SCALE MISMATCH" in text and "P001.ome.tiff" in text
    assert "0.2125" in text and "0.325" in text


def test_float_noise_is_not_a_disagreement(caplog):
    """OME headers routinely serialise 0.325 as 0.32499998807907104."""
    with caplog.at_level(logging.WARNING):
        fired = ps.warn_on_pixel_size_mismatch(
            0.32499998807907104, 0.325, source="x", logger=logging.getLogger()
        )
    assert not fired
    assert "SCALE MISMATCH" not in caplog.text


def test_absent_scale_is_not_a_disagreement(caplog):
    """Warning here would train operators to ignore the warning that matters."""
    with caplog.at_level(logging.WARNING):
        fired = ps.warn_on_pixel_size_mismatch(
            (None, None), 0.325, source="x", logger=logging.getLogger()
        )
    assert not fired
    assert caplog.text == ""


def test_one_axis_disagreeing_is_enough(caplog):
    with caplog.at_level(logging.WARNING):
        fired = ps.warn_on_pixel_size_mismatch(
            (0.325, 0.65), 0.325, source="x", logger=logging.getLogger()
        )
    assert fired
    assert caplog.text.count("SCALE MISMATCH") == 1  # only the Y axis


def test_never_returns_a_scale_to_use():
    """The signature is the guard: a caller cannot accidentally prefer the metadata.

    This is what keeps `params.pixel_size` the single owner. A function that returned
    "the resolved pixel size" would invite exactly the drift this module exists to make
    visible.
    """
    assert ps.warn_on_pixel_size_mismatch(
        0.1, 0.325, source="x", logger=logging.getLogger()
    ) in (True, False)


# ── the unit travels with the value ────────────────────────────────────────────
# A measurement is a number AND a unit. Passing the number alone across a seam is how
# `coarse_tre_px` came to be rendered under a "(µm)" header with no conversion on the
# path: the field names disagreed, but nothing enforced them, so a naming convention
# was the only thing standing between a pixel count and a micrometre figure. These
# types make that mismatch a TypeError instead.


def test_to_microns_scales_a_pixel_count_by_the_configured_size():
    assert ps.to_microns(ps.Pixels(4.0), 0.325).value == pytest.approx(1.3)


def test_to_microns_is_the_identity_on_a_value_already_in_microns():
    assert ps.to_microns(ps.Microns(1.3), 0.325) == ps.Microns(1.3)


def test_a_bare_float_is_not_a_measurement():
    """The whole point: an unlabelled number cannot be assigned into a µm slot."""
    with pytest.raises(TypeError):
        ps.to_microns(4.0, 0.325)


def test_a_value_whose_producer_recorded_no_unit_cannot_be_converted():
    with pytest.raises(TypeError):
        ps.to_microns(ps.Unrecorded(4.0), 0.325)


def test_a_pixel_count_cannot_be_converted_without_a_scale():
    """No scale means no µm figure -- not a plausible one at some assumed default."""
    with pytest.raises(TypeError):
        ps.to_microns(ps.Pixels(4.0), None)
    with pytest.raises(TypeError):
        ps.to_microns(ps.Pixels(4.0), 0.0)


def test_to_microns_takes_the_scale_as_an_argument_rather_than_detecting_one():
    """Conversion, not detection.

    `warn_on_pixel_size_mismatch` deliberately never returns a scale so that no caller
    can start preferring an image's own metadata over `params.pixel_size`. `to_microns`
    keeps that property: it reads nothing, so the only scale it can use is the one the
    caller was handed.
    """
    import inspect

    src = inspect.getsource(ps.to_microns)
    assert "read_ome_pixel_size" not in src
    assert "pixel_size_um" in inspect.signature(ps.to_microns).parameters


def test_each_measurement_renders_with_its_unit():
    assert ps.Pixels(4.0).unit == "px"
    assert ps.Microns(1.3).unit == "µm"
    assert ps.Unrecorded(4.0).unit == "unrecorded"


def test_the_base_measurement_cannot_be_instantiated():
    """`Measurement` is the isinstance base and the type alias, not a usable value.

    An instantiable base is a fourth, unit-less measurement type hiding in the
    vocabulary: `to_microns(Measurement(4.0))` used to report "is a bare number, not a
    measurement", which is false about the very object it was handed.
    """
    with pytest.raises(TypeError, match="abstract"):
        ps.Measurement(4.0)


def test_the_unit_refusal_exception_is_dedicated_and_still_a_type_error():
    """Dedicated so a caller can catch it WITHOUT swallowing a genuine coding error;
    still a TypeError so the brief's "make the mismatch a type error" holds."""
    assert issubclass(ps.UnitError, TypeError)
    with pytest.raises(ps.UnitError):
        ps.to_microns(ps.Pixels(4.0), None)
    with pytest.raises(ps.UnitError):
        ps.to_microns(4.0, 0.325)
    with pytest.raises(ps.UnitError):
        ps.to_microns(ps.Unrecorded(4.0), 0.325)
