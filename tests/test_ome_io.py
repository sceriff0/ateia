"""Unit tests for bin/utils/ome_io.py -- the pipeline's one image I/O module.

WHY THE IMPORTS BELOW LOOK LIKE THIS. `ome_io` is a bin/utils module, and the
convention every bin/ script follows is `sys.path.insert(0, .../bin/utils)`
followed by a flat `from ome_io import ...`. Both inserts are needed here (bin
for `from bin import ...` style helpers elsewhere, bin/utils for the flat
sibling imports ome_io itself performs), matching tests/test_tiled_io.py.

WHAT IS DELIBERATELY NOT TESTED HERE. `bioio`, `bioio-bioformats` and `h5py`
are installed in NO CI environment (requirements/ci.txt installs none of the
three), so every test in this file must run without them. That is exactly why
ome_io imports all three lazily, inside functions: importing this module must
never require them. Plan 07 writes the format-specific tests on synthesised
fixtures; this file tests ome_io itself.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
tifffile = pytest.importorskip("tifffile")

import ome_io  # noqa: E402

# ---------------------------------------------------------------------------
# detect_reader -- the format dispatch table
# ---------------------------------------------------------------------------

BIOIO = [".ome.tif", ".ome.tiff", ".tif", ".tiff", ".nd2", ".czi", ".lif"]
TIFFFILE = [".ndpi", ".ndpis"]
HDF5 = [".h5", ".hdf5"]
BIOFORMATS = [".svs", ".qptiff", ".vsi", ".scn", ".mrxs", ".bif", ".ims"]


@pytest.mark.parametrize("suffix", BIOIO)
def test_bioio_native_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "bioio"


@pytest.mark.parametrize("suffix", TIFFFILE)
def test_hamamatsu_suffixes_go_to_tifffile(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "tifffile"


@pytest.mark.parametrize("suffix", HDF5)
def test_hdf5_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "hdf5"


@pytest.mark.parametrize("suffix", BIOFORMATS)
def test_bioformats_only_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "bioio-bioformats"


def test_every_answer_is_one_of_READERS():
    for suffix in BIOIO + TIFFFILE + HDF5 + BIOFORMATS:
        assert ome_io.detect_reader(f"/x/s{suffix}") in ome_io.READERS


def test_the_dispatch_is_case_insensitive():
    assert ome_io.detect_reader("/x/SLIDE.SVS") == "bioio-bioformats"
    assert ome_io.detect_reader("/x/SLIDE.OME.TIFF") == "bioio"


def test_an_unclaimed_suffix_raises_and_names_it():
    """The whole point of replacing convert_image.get_file_format's silent
    `return "bioio"` fall-through: a .png used to be handed to BioImage and
    failed several frames deep, naming the wrong problem."""
    with pytest.raises(ome_io.UnsupportedFormatError) as exc:
        ome_io.detect_reader("/x/thumbnail.png")
    message = str(exc.value)
    assert ".png" in message, "the message must name the suffix that was refused"
    assert ".czi" in message, "the message must list what IS supported"


def test_a_file_with_no_suffix_raises():
    with pytest.raises(ome_io.UnsupportedFormatError):
        ome_io.detect_reader("/x/slide")


def test_UnsupportedFormatError_is_a_ValueError():
    """Callers that already catch ValueError around a read keep working."""
    assert issubclass(ome_io.UnsupportedFormatError, ValueError)


# ---------------------------------------------------------------------------
# require_reader -- the plugin precondition
# ---------------------------------------------------------------------------


def test_require_reader_accepts_a_reader_whose_modules_are_installed():
    ome_io.require_reader("tifffile")  # must not raise


def test_require_reader_names_the_missing_plugin():
    """`.svs` routes to bioio-bioformats, which containers/convert installs in
    phase 06 and nothing installs before it. The failure has to say the plugin's
    name, not `ModuleNotFoundError: bioio_bioformats` several frames deep."""
    if ome_io._module_is_importable("bioio_bioformats"):
        pytest.skip("bioio-bioformats is installed here; nothing to assert")
    with pytest.raises(ImportError) as exc:
        ome_io.require_reader("bioio-bioformats")
    assert "bioio-bioformats" in str(exc.value)


def test_require_reader_rejects_an_unknown_reader_name():
    with pytest.raises(ome_io.UnsupportedFormatError):
        ome_io.require_reader("openslide")


# ---------------------------------------------------------------------------
# ome_metadata -- the dict four scripts used to rebuild independently
# ---------------------------------------------------------------------------


def test_ome_metadata_key_order_is_the_shape_tifffile_serialises():
    """Key ORDER is asserted, not just contents. tifffile builds the OME-XML by
    walking this dict, so a reordering changes the attribute order in the header
    of every file this pipeline writes -- which is a diff in a published artifact
    for no behavioural reason. Pinned to the order convert_image.py used."""
    md = ome_io.ome_metadata(["DAPI", "CD3"], 0.325)
    assert list(md) == [
        "axes",
        "Channel",
        "PhysicalSizeX",
        "PhysicalSizeXUnit",
        "PhysicalSizeY",
        "PhysicalSizeYUnit",
    ]
    assert md["axes"] == "CYX"
    assert md["Channel"] == {"Name": ["DAPI", "CD3"]}
    assert md["PhysicalSizeX"] == 0.325
    assert md["PhysicalSizeXUnit"] == "µm"
    assert md["PhysicalSizeY"] == 0.325


def test_ome_metadata_accepts_separate_x_and_y():
    md = ome_io.ome_metadata(["DAPI"], (0.5, 0.25))
    assert md["PhysicalSizeX"] == 0.5
    assert md["PhysicalSizeY"] == 0.25


def test_ome_metadata_omits_physical_size_when_there_is_none():
    """None means "the file did not say". Writing a placeholder number would be a
    scale the pipeline invented -- bin/utils/pixel_size.py exists to stop exactly
    that."""
    md = ome_io.ome_metadata(["DAPI"], None)
    assert "PhysicalSizeX" not in md
    assert "PhysicalSizeXUnit" not in md
    assert md["Channel"] == {"Name": ["DAPI"]}


def test_ome_metadata_omits_channel_names_when_there_are_none():
    md = ome_io.ome_metadata(None, 0.325)
    assert "Channel" not in md
    assert md["PhysicalSizeX"] == 0.325


def test_ome_metadata_carries_z_when_asked():
    md = ome_io.ome_metadata(["DAPI"], 0.325, axes="CZYX", pixel_size_z_um=1.5)
    assert md["axes"] == "CZYX"
    assert md["PhysicalSizeZ"] == 1.5
    assert md["PhysicalSizeZUnit"] == "µm"


# ---------------------------------------------------------------------------
# write_ome_tiff -- the round trip
# ---------------------------------------------------------------------------


def _stack(c=3, h=24, w=20, dtype=np.uint16, seed=11):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 4000, size=(c, h, w)).astype(dtype)


def test_write_ome_tiff_round_trips_pixels_channels_scale_and_dtype(tmp_path):
    data = _stack()
    out = tmp_path / "round_trip.ome.tif"

    ome_io.write_ome_tiff(
        out, data, channels=["DAPI", "CD3", "CD8"], pixel_size_um=0.325, tile=16
    )

    back = tifffile.imread(out)
    assert back.shape == data.shape
    assert back.dtype == data.dtype
    np.testing.assert_array_equal(back, data)

    info = ome_io.read_info(out) if hasattr(ome_io, "read_info") else None
    with tifffile.TiffFile(out) as tf:
        xml = tf.ome_metadata
    assert xml, "write_ome_tiff must produce an OME header"
    for name in ("DAPI", "CD3", "CD8"):
        assert f'Name="{name}"' in xml
    assert 'PhysicalSizeX="0.325"' in xml
    assert 'PhysicalSizeXUnit="µm"' in xml
    assert info is None or info.channels == ["DAPI", "CD3", "CD8"]


def test_write_ome_tiff_writes_tiled_at_the_requested_tile(tmp_path):
    """A striped write makes every downstream region read decode a whole plane --
    the measurement in CONVERT_TIFF_TILE's comment. Checked on the TIFF tags AND
    on the zarr chunk shape the pipeline's own reader (tiled_io.open_lazy) sees,
    because the tags could in principle be present without the zarr view
    honouring them."""
    zarr = pytest.importorskip("zarr")
    data = _stack(c=2, h=40, w=40)
    out = tmp_path / "tiled.ome.tif"

    ome_io.write_ome_tiff(out, data, channels=["A", "B"], pixel_size_um=0.5, tile=16)

    with tifffile.TiffFile(out) as tf:
        page = tf.pages[0]
        assert page.is_tiled
        assert (page.tilelength, page.tilewidth) == (16, 16)

    store = tifffile.imread(str(out), aszarr=True)
    try:
        grp = zarr.open(store, mode="r")
        arr = grp[0] if isinstance(grp, zarr.Group) else grp
        assert arr.chunks == (1, 16, 16)
    finally:
        store.close()


def test_write_ome_tiff_handles_a_plane_larger_than_one_tile(tmp_path):
    """tifffile's iterator mode is TILE-wise whenever tile= is set: it walks
    numtiles items per page and raises ValueError('tile is too large') the moment
    one is bigger than a tile. A plane-fed writer therefore passes on any fixture
    smaller than one tile and fails on the first real slide. 24x20 over a 16px
    tile is 2x2 tiles, three of them short -- so this also exercises the partial
    edge tiles tifffile pads."""
    data = _stack(c=2, h=24, w=20)
    out = tmp_path / "big_plane.ome.tif"

    ome_io.write_ome_tiff(out, data, channels=["A", "B"], pixel_size_um=None, tile=16)

    np.testing.assert_array_equal(tifffile.imread(out), data)


def test_write_ome_tiff_defaults_bigtiff_off_below_the_threshold(tmp_path):
    """bigtiff=None means "BigTIFF iff nbytes > 2**31". A tiny stack must not
    silently become BigTIFF: a classic TIFF is what every reader accepts."""
    out = tmp_path / "small.ome.tif"
    ome_io.write_ome_tiff(
        out, _stack(), channels=["a", "b", "c"], pixel_size_um=0.325, tile=16
    )
    with tifffile.TiffFile(out) as tf:
        assert tf.is_bigtiff is False


def test_write_ome_tiff_switches_to_bigtiff_above_the_threshold(tmp_path):
    """Asserted on the DECISION function rather than by writing a 2 GB file:
    materialising one in a unit test is not proportionate, and the threshold is
    the part that can regress."""
    assert ome_io._wants_bigtiff(None, 2**31 + 1) is True
    assert ome_io._wants_bigtiff(None, 2**31) is False
    assert ome_io._wants_bigtiff(True, 0) is True
    assert ome_io._wants_bigtiff(False, 2**40) is False


def test_write_ome_tiff_writes_pyramid_levels_as_subifds(tmp_path):
    """pyramid_levels=2 means base + one reduced level, carried as subIFDs of the
    base page (the layout QuPath reads), not as extra top-level series."""
    data = _stack(c=2, h=64, w=64)
    out = tmp_path / "pyramid.ome.tif"

    ome_io.write_ome_tiff(
        out, data, channels=["A", "B"], pixel_size_um=0.325, tile=16, pyramid_levels=2
    )

    with tifffile.TiffFile(out) as tf:
        base = tf.series[0]
        assert len(base.levels) == 2, (
            f"expected base + 1 reduced level, got {len(base.levels)}"
        )
        assert base.levels[1].shape[-2:] == (32, 32)
        np.testing.assert_array_equal(base.levels[0].asarray(), data)


def test_write_ome_tiff_writes_pyramid_levels_on_an_odd_non_square_plane(tmp_path):
    """The reshape arithmetic that derives each reduced level's shape must not
    assume an even, square plane. 37x51 (both odd, both different) forces
    ceil-division: level 1 must be (19, 26), i.e. ceil(37/2) x ceil(51/2)."""
    data = _stack(c=3, h=37, w=51)
    out = tmp_path / "odd_pyramid.ome.tif"

    ome_io.write_ome_tiff(
        out,
        data,
        channels=["A", "B", "C"],
        pixel_size_um=0.25,
        tile=16,
        pyramid_levels=2,
    )

    with tifffile.TiffFile(out) as tf:
        base = tf.series[0]
        assert len(base.levels) == 2
        assert base.levels[1].shape[-2:] == (19, 26)
        np.testing.assert_array_equal(base.levels[0].asarray(), data)
        np.testing.assert_array_equal(base.levels[1].asarray(), data[:, ::2, ::2])


def test_write_ome_tiff_rejects_a_channel_count_that_does_not_match_the_data(tmp_path):
    """Naming three channels on a two-channel stack writes a header that lies, and
    every downstream consumer reads the names positionally."""
    with pytest.raises(ValueError, match="channel"):
        ome_io.write_ome_tiff(
            tmp_path / "bad.ome.tif",
            _stack(c=2),
            channels=["A", "B", "C"],
            pixel_size_um=0.325,
            tile=16,
        )


def test_write_ome_tiff_never_materialises_a_lazy_stack(tmp_path):
    """The property the streaming write exists for. An array-like whose
    __array__ raises stands in for a lazy handle; a writer that still calls
    numpy.asarray on the whole stack trips it."""

    class _RefusesWholeArray:
        def __init__(self, array):
            self._array = array
            self.shape = array.shape
            self.dtype = array.dtype
            self.ndim = array.ndim

        def __getitem__(self, index):
            return self._array[index]

        def __array__(self, *_args, **_kwargs):
            raise AssertionError("the writer materialised the whole stack")

    data = _stack(c=3, h=24, w=20)
    out = tmp_path / "lazy.ome.tif"
    ome_io.write_ome_tiff(
        out,
        _RefusesWholeArray(data),
        channels=["a", "b", "c"],
        pixel_size_um=0.325,
        tile=16,
    )
    np.testing.assert_array_equal(tifffile.imread(out), data)


# ---------------------------------------------------------------------------
# write_tiff -- the non-OME escape hatch
# ---------------------------------------------------------------------------


def test_write_tiff_forwards_its_kwargs_verbatim(tmp_path):
    """write_tiff sets NO defaults of its own. Every migrated call site keeps the
    exact keyword arguments it used to pass tifffile.imwrite, so the migration is
    behaviour-preserving by construction."""
    mask = np.arange(256, dtype=np.uint32).reshape(16, 16)
    out = tmp_path / "mask.tif"

    returned = ome_io.write_tiff(
        out, mask, compression="zlib", bigtiff=True, tile=(16, 16)
    )

    assert returned == out
    np.testing.assert_array_equal(tifffile.imread(out), mask)
    with tifffile.TiffFile(out) as tf:
        assert tf.is_bigtiff
        assert tf.pages[0].is_tiled


def test_write_tiff_does_not_force_bigtiff(tmp_path):
    """An imagej=True write (bin/utils/qc.py's composite) is INCOMPATIBLE with
    BigTIFF -- tifffile refuses the combination. A default of bigtiff=True here
    would have broken that call site the day it was migrated."""
    rgb = np.zeros((3, 8, 8), dtype=np.uint8)
    out = tmp_path / "composite.tif"
    ome_io.write_tiff(
        out, rgb, imagej=True, metadata={"axes": "CYX", "mode": "composite"}
    )
    with tifffile.TiffFile(out) as tf:
        assert tf.is_bigtiff is False
