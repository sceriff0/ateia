"""`PhysicalSizeX` was read without `PhysicalSizeXUnit`.

bin/ashlar_retile.py's `_pixel_size_um` did `float(pixels.get("PhysicalSizeX"))`
and returned it as µm/px. OME-XML records the unit alongside the value and
writers do use more than µm -- a scanner writing nm is legal and common -- so a
header saying `PhysicalSizeX="325" PhysicalSizeXUnit="nm"` was read as 325 µm/px
instead of 0.325. That is a 1000x scale error, and it goes straight into
ASHLAR's `--maximum-shift`, which is expressed in µm and converted to pixels
using exactly this number.

The repo already had the correct reader. bin/utils/pixel_size.py carries a
unit table and a `_to_um` conversion, and read_ome_pixel_size has used it since
it was written; ashlar_retile.py simply never called it. So the fix is not a
second unit table -- a second table is the forkable-private-parse defect this
whole engagement is closing -- it is one exported conversion with two callers.

The two readers still differ on ONE point, deliberately, and this file pins the
difference so it reads as a decision rather than an oversight:

    read_ome_pixel_size  returns None on an unrecognised unit. Its result is
                         only ever used to WARN; params.pixel_size stays
                         authoritative, so "unknown" costs nothing.
    _pixel_size_um       raises. Its result IS the authoritative scale -- there
                         is nothing to fall back to that would be any more
                         right -- so falling back to DEFAULT_PIXEL_SIZE_UM
                         would discard a perfectly good calibration written in
                         a scale we did not recognise, silently, which is the
                         defect class being closed.
"""
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import tifffile
from ashlar_retile import DEFAULT_PIXEL_SIZE_UM, _pixel_size_um

OME_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
    '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" '
    'Type="uint16" SizeX="4" SizeY="4" SizeZ="1" SizeC="1" SizeT="1"{attrs}>'
    '<Channel ID="Channel:0" SamplesPerPixel="1"/>'
    '<TiffData/></Pixels></Image></OME>'
)


def _write(tmp_path, name, attrs):
    """A 4x4 uint16 TIFF whose OME header carries exactly `attrs`."""
    path = tmp_path / name
    tifffile.imwrite(
        path,
        np.zeros((4, 4), dtype=np.uint16),
        description=OME_TEMPLATE.format(attrs=attrs),
        photometric="minisblack",
    )
    # Guard the fixture itself: if tifffile stops round-tripping the header,
    # every case below would fall through to DEFAULT_PIXEL_SIZE_UM and pass for
    # the wrong reason.
    with tifffile.TiffFile(path) as tif:
        assert tif.ome_metadata, f"{name}: no OME header survived the write"
        if "PhysicalSizeX=" in attrs:
            pixels = ET.fromstring(tif.ome_metadata).find(".//{*}Pixels")
            assert pixels is not None and pixels.get("PhysicalSizeX"), (
                f"{name}: PhysicalSizeX did not survive the write"
            )
    return path


# (unit as it appears in the XML, expected µm/px for a raw value of 325, test id)
#
# The micro sign is written as the XML character reference `&#181;`, not as a
# literal µ: TIFF's ImageDescription tag is ASCII, so tifffile refuses a literal
# one and real OME writers use the entity. ElementTree decodes it back to µ, so
# the reader sees the same string it would in the field.
UNIT_CASES = [
    ('&#181;m', 325.0, "micro-sign-entity"),
    ('um', 325.0, "um"),
    ('micron', 325.0, "micron"),
    ('nm', 0.325, "nm"),             # the live defect: 1000x too large before the fix
    ('mm', 325_000.0, "mm"),
    (None, 325.0, "absent"),         # OME's default is µm, per the 2016-06 schema
]


@pytest.mark.parametrize(
    "unit,expected", [(u, e) for u, e, _i in UNIT_CASES],
    ids=[i for _u, _e, i in UNIT_CASES],
)
def test_the_value_comes_back_in_um_whatever_the_header_says(tmp_path, unit, expected):
    attrs = ' PhysicalSizeX="325"'
    if unit is not None:
        attrs += f' PhysicalSizeXUnit="{unit}"'
    name = f"u_{(unit or 'absent').replace('&#181;', 'micro').replace(';', '')}.ome.tif"
    got = _pixel_size_um(_write(tmp_path, name, attrs))
    assert got == pytest.approx(expected), (
        f"PhysicalSizeX=325 {unit!r} read as {got} um/px, expected {expected}"
    )


def test_the_nm_case_is_not_merely_the_fallback(tmp_path):
    """0.325 is also DEFAULT_PIXEL_SIZE_UM. Without this, a reader that gave up
    on the nm header entirely would pass the parametrised case above for
    exactly the wrong reason."""
    path = _write(
        tmp_path, "nm_650.ome.tif",
        ' PhysicalSizeX="650" PhysicalSizeXUnit="nm"',
    )
    assert _pixel_size_um(path) == pytest.approx(0.650)
    assert DEFAULT_PIXEL_SIZE_UM != pytest.approx(0.650)


def test_an_unrecognised_unit_refuses_rather_than_guessing(tmp_path):
    """A unit we cannot interpret means the file HAS a calibration we failed to
    read -- not that it has none. Falling back to DEFAULT_PIXEL_SIZE_UM would
    discard it silently, and this number is what ASHLAR's --maximum-shift is
    scaled by."""
    path = _write(
        tmp_path, "bad_unit.ome.tif",
        ' PhysicalSizeX="325" PhysicalSizeXUnit="furlong"',
    )
    with pytest.raises(ValueError) as exc:
        _pixel_size_um(path)
    assert "furlong" in str(exc.value)


def test_genuine_absence_still_falls_back_silently(tmp_path):
    """The pre-existing contract, unchanged: no PhysicalSizeX at all is
    ordinary -- most intermediates in this pipeline carry no scale."""
    assert _pixel_size_um(_write(tmp_path, "none.ome.tif", "")) == DEFAULT_PIXEL_SIZE_UM


def test_a_non_numeric_value_still_falls_back_with_a_warning(tmp_path, caplog):
    """Also pre-existing, and distinct from an unknown unit: there is no
    calibration to discard, only a corrupt one."""
    path = _write(
        tmp_path, "nan.ome.tif", ' PhysicalSizeX="not-a-number"',
    )
    with caplog.at_level("WARNING"):
        assert _pixel_size_um(path) == DEFAULT_PIXEL_SIZE_UM
    assert any("not-a-number" in r.getMessage() for r in caplog.records), (
        "the corrupt value must be named in the log"
    )


def test_both_readers_share_one_unit_table():
    """The point of the fix. Two private unit tables would drift, and the drift
    would be a silent scale error in whichever one fell behind."""
    import inspect

    import ashlar_retile
    from pixel_size import unit_to_um

    # The import is function-local by design -- pixel_size pulls in tifffile at
    # module scope and ashlar_retile defers it so `--help` stays cheap -- so
    # this is asserted on the source rather than as a module attribute.
    src = inspect.getsource(ashlar_retile)
    assert "from pixel_size import unit_to_um" in src, (
        "ashlar_retile has stopped importing pixel_size.unit_to_um"
    )
    # A dict ENTRY for a unit, not a mention of one: the docstring quotes
    # PhysicalSizeXUnit="nm" as the concrete defect, and a check that could not
    # tell those apart would have to be deleted the first time someone
    # documented the bug they had just fixed.
    import re as _re
    assert not _re.search(r"""["'](?:nm|mm|um|micron)["']\s*:""", src), (
        "ashlar_retile appears to carry its own unit table again. Two tables "
        "drift, and the drift is a silent scale error in whichever copy falls "
        "behind -- which is the defect this fix closed."
    )
    # And the shared table really does convert, rather than being an empty dict
    # that would make every unit unknown and every case above raise.
    assert unit_to_um("nm") == pytest.approx(1e-3)
    assert unit_to_um("µm") == pytest.approx(1.0)
    assert unit_to_um(None) == pytest.approx(1.0), "absent unit means µm per OME"
    assert unit_to_um("furlong") is None
