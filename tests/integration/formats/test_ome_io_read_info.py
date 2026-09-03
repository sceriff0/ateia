"""`ome_io.read_info` against every format the generator can synthesise.

RUNS ONLY IN THE `format-tests` CI JOB. tests/integration/ is excluded from the
standard pytest invocation, and that exclusion is load-bearing here: this file
needs requirements/format-tests.txt's reader stack (bioio + bioio-ome-tiff at
containers/convert's pins), which cannot coexist with the harmonised tifffile
the main suite runs on. See requirements/format-tests.txt's header for the
measurement.

Everything is asserted through ome_io's published interface -- never through
bin/convert_image.py's internals -- so a reader swap behind read_info is free.

IMPORT CONVENTION. `ome_io` is a bin/utils module and imports its siblings
(`jvm_cache`, `pixel_size`) with a flat `from jvm_cache import ...`, so both
`bin` and `bin/utils` must be on `sys.path` -- matching tests/test_ome_io.py
and tests/test_tiled_io.py, not a plain `from utils import ome_io`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# parents[0]=formats, [1]=integration, [2]=tests, [3]=repo root.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "bin" / "utils"))

import ome_io  # noqa: E402


def _fixture(rel: str) -> Path:
    """`rel` is the REPO-RELATIVE LITERAL, e.g. `"tests/testdata/fmt_pyramid.ome.tiff"` --
    not a bare filename joined onto a `TESTDATA` constant at call time.
    tests/test_fixtures_have_a_producer.py's `_REF` regex greps for the literal
    substring `tests/testdata/<name>` in THIS FILE'S OWN SOURCE TEXT; a `TESTDATA / name`
    join built from a bare filename never renders that substring anywhere on disk, so the
    guard cannot see these fixtures at all -- it stayed green when a reviewer deleted
    fmt_rgb.tiff's producer line from the generator entirely."""
    assert rel.startswith("tests/testdata/"), (
        f"{rel!r} must be the repo-relative literal 'tests/testdata/<name>', not a bare "
        "filename -- see this function's docstring for why."
    )
    path = ROOT / rel
    assert path.exists(), (
        f"{path} is missing. Run `python tests/testdata/generate_complete_testdata.py` "
        "-- tests/testdata/ is gitignored, so every fixture exists only because the "
        "generator wrote it."
    )
    return path


def test_a_pyramidal_ome_tiff_reads_its_FULL_resolution_level():
    """A reader that hands back the lowest pyramid level instead of level 0
    halves every coordinate downstream, silently. The fixture's levels differ
    (256 vs 128), so the wrong one is visible in the shape."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_pyramid.ome.tiff"))
    assert info.shape_cyx == (2, 256, 256)
    assert info.dtype == np.dtype("uint16")
    assert list(info.channels) == ["DAPI", "CD3"]
    assert info.pixel_size_um == pytest.approx(0.5)
    assert info.reader == "bioio"


def test_a_bigtiff_input_reads_back_whole():
    """The pipeline WRITES BigTIFF unconditionally; nothing read one until now,
    so the 8-byte-offset path was covered on one side only."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_bigtiff.ome.tiff"))
    assert info.shape_cyx == (2, 64, 64)
    assert info.dtype == np.dtype("uint16")
    assert list(info.channels) == ["DAPI", "CD3"]
    assert info.pixel_size_um == pytest.approx(0.5)


def test_an_interleaved_rgb_tiff_presents_three_channels():
    """bioio reports this file as TCZYXS with C=1 and S=3. read_info's contract
    is shape_cyx, so the S axis must arrive as the channel axis -- this is the
    only fixture that exercises the S-as-C remap on real bytes."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_rgb.tiff"))
    assert info.shape_cyx == (3, 64, 64)
    assert info.dtype == np.dtype("uint8")
    # read_info's contract distinguishes a REAL 3-channel file from three colour
    # SAMPLES of one channel -- a caller (e.g. a channel-name default, or a decision
    # about whether --channels is required) needs to tell them apart.
    assert info.channels_are_samples is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, not yet fixed (scheduled for plan 07 Tasks 5-6): read_plane's "
        "tifffile branch (open_lazy's (C,H,W) view and the tifffile.imread(...)"
        "[channel] fallback) does not apply the S-as-C remap read_info already "
        "performs. Measured: read_plane('fmt_rgb.tiff', 1) currently returns shape "
        "(64, 3) -- axis 0 of the raw YXS array (a single ROW, all three samples) -- "
        "instead of the (64, 64) single-sample plane the contract promises. "
        "strict=True: the moment open_lazy gains S-axis awareness this must start "
        "failing (unexpectedly passing), which is the signal to delete the xfail "
        "and turn this into a real assertion."
    ),
)
def test_read_plane_on_an_interleaved_rgb_tiff():
    plane = ome_io.read_plane(_fixture("tests/testdata/fmt_rgb.tiff"), 1)
    assert plane.ndim == 2
    assert plane.shape == (64, 64)


def test_an_eight_bit_single_channel_tiff_keeps_its_dtype():
    info = ome_io.read_info(_fixture("tests/testdata/fmt_gray8.tiff"))
    assert info.shape_cyx == (1, 64, 64)
    assert info.dtype == np.dtype("uint8")


def test_a_float32_single_channel_tiff_keeps_its_dtype():
    """A deconvolved or ratiometric channel arrives as float32. Every other
    image fixture in this repo is uint16."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_float32.tiff"))
    assert info.shape_cyx == (1, 64, 64)
    assert info.dtype == np.dtype("float32")


def test_a_plain_tiff_carries_no_channel_names_rather_than_inventing_them():
    """`channels` is `list[str] | None` and None means 'the file did not say'.
    A reader that fabricated names here would make a samplesheet's declared
    channels look confirmed when nothing confirmed them."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_gray8.tiff"))
    assert info.channels is None or all(
        name.startswith("Channel:") for name in info.channels
    ), f"a plain TIFF reported real-looking channel names: {info.channels}"


def test_read_plane_returns_one_two_dimensional_plane():
    plane = ome_io.read_plane(_fixture("tests/testdata/fmt_pyramid.ome.tiff"), 1)
    assert plane.ndim == 2
    assert plane.shape == (256, 256)


def test_a_truncated_file_raises_rather_than_returning_a_partial_read():
    """100 bytes: past the TIFF header, short of the first IFD. The failure must
    be an exception, not an empty array -- an empty array becomes an all-zero
    slide four processes later."""
    with pytest.raises(Exception):
        ome_io.read_info(_fixture("tests/testdata/fmt_truncated.ome.tiff"))


def test_an_hdf5_slide_reads_its_shape_channels_and_scale():
    """read_image_h5 and _extract_h5_pixel_sizes had no test at all. The fixture
    nests the dataset one group deep on purpose: _find_first_image_dataset
    recurses, and a flat file would not exercise that."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_image.h5"))
    assert info.reader == "hdf5"
    assert info.shape_cyx == (3, 64, 64)
    assert info.dtype == np.dtype("uint16")
    assert list(info.channels) == ["DAPI", "PANCK", "SMA"]
    # element_size_um is ZYX-ordered: [1.0, 0.5, 0.5] -> Y=0.5, X=0.5.
    assert info.pixel_size_um == pytest.approx(0.5)


def test_a_single_ndpi_reads_through_tifffile_with_its_scan_scale():
    """.ndpi routes to tifffile, never bioio -- and it cannot be faked by
    renaming a TIFF (tifffile picks the 64-bit-offset format from the extension
    alone, giving zero pages). See the generator's write_minimal_ndpi."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_cy5.ndpi"))
    assert info.reader == "tifffile"
    assert info.shape_cyx == (1, 64, 64)
    assert info.dtype == np.dtype("uint16")
    # 20000 px/cm at RESUNIT.CENTIMETER -> 10000/20000 = 0.5 um, exactly.
    assert info.pixel_size_um == pytest.approx(0.5)


def test_an_ndpis_manifest_stacks_its_ndpi_files_into_channels():
    """The manifest is the multi-channel Hamamatsu shape: one .ndpi per channel,
    stacked into CYX. Its hand-written INI parsing had zero tests."""
    info = ome_io.read_info(_fixture("tests/testdata/fmt_set.ndpis"))
    assert info.reader == "tifffile"
    assert info.shape_cyx == (2, 64, 64)
    assert info.pixel_size_um == pytest.approx(0.5)
