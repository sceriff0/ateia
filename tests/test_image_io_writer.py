#!/usr/bin/env python3
"""Behavioural tests for bin/utils/image_io.py, the one owner of TIFF writes.

The static guard (``tests/test_image_io_ownership.py``) proves every write *goes
through* this module and that every call in it passes ``bigtiff=True``. That is a
check on the source text. These tests check the bytes on disk: a file written by each
entry point actually reports ``is_bigtiff``, actually carries compression where the
variant promises it, and round-trips values and dtype unchanged.

Both halves are needed. ``bigtiff=True`` in the source proves nothing if tifffile
silently ignores it for some shape or some pin, and a round-trip proves nothing about
code paths no test happens to call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN / "utils"))

tifffile = pytest.importorskip("tifffile")

import image_io  # noqa: E402


# ── masks: the defect class this module exists to make unwritable ──────────────
@pytest.mark.parametrize("dtype", [np.uint16, np.uint32])
def test_mask_write_round_trips_with_bigtiff_and_compression(tmp_path, dtype) -> None:
    rng = np.random.default_rng(0)
    mask = rng.integers(0, 5000, size=(64, 48), dtype=np.uint32).astype(dtype)

    out = image_io.write_mask_tiff(tmp_path / "cell_mask.tif", mask)

    assert out.exists()
    with tifffile.TiffFile(out) as tif:
        assert tif.is_bigtiff, "a full-resolution label mask must be BigTIFF"
        page = tif.pages[0]
        assert page.compression != tifffile.COMPRESSION.NONE, (
            "extract_mask_series.py used to write uncompressed uint32 masks -- the "
            "worst of the pre-Task-11 sites; compression is not optional here"
        )
    read_back = tifffile.imread(out)
    assert read_back.dtype == mask.dtype
    np.testing.assert_array_equal(read_back, mask)


def test_mask_write_creates_missing_parent_directories(tmp_path) -> None:
    mask = np.zeros((8, 8), dtype=np.uint32)
    out = image_io.write_mask_tiff(tmp_path / "deep" / "er" / "m.tif", mask)
    assert out.exists()


def test_mask_write_accepts_a_multi_channel_stack(tmp_path) -> None:
    """EXTRACT_MASK_SERIES writes two single-plane masks; MERGE writes a (2, H, W)
    stack. Both shapes must work, and neither may lose a plane."""
    stack = np.stack([np.full((16, 16), 7, np.uint32), np.full((16, 16), 9, np.uint32)])
    out = image_io.write_mask_tiff(tmp_path / "stack.tif", stack)
    np.testing.assert_array_equal(tifffile.imread(out), stack)


# ── OME variant ────────────────────────────────────────────────────────────────
def test_ome_write_is_bigtiff_and_keeps_channel_names_and_order(tmp_path) -> None:
    data = np.stack(
        [np.full((32, 32), i, np.uint16) for i in range(3)]
    )  # channel i is filled with i, so a reorder is detectable
    out = image_io.write_ome_tiff(
        tmp_path / "img.ome.tif",
        data,
        metadata={
            "axes": "CYX",
            "Channel": {"Name": ["DAPI", "CD3", "CD8"]},
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
        },
        compression="zlib",
        tile=(16, 16),
    )
    with tifffile.TiffFile(out) as tif:
        assert tif.is_bigtiff
        assert tif.is_ome and tif.ome_metadata
        assert "DAPI" in tif.ome_metadata and "CD8" in tif.ome_metadata
    read_back = tifffile.imread(out)
    assert read_back.dtype == np.uint16
    np.testing.assert_array_equal(read_back, data)


def test_ome_write_without_compression_or_tiling_is_still_bigtiff(tmp_path) -> None:
    """CONVERT_IMAGE writes uncompressed and untiled on purpose; bigtiff is not
    negotiable even in the variant that turns everything else off."""
    data = np.zeros((2, 16, 16), np.uint16)
    out = image_io.write_ome_tiff(
        tmp_path / "c.ome.tif", data, metadata={"axes": "CYX"}, compression=None
    )
    with tifffile.TiffFile(out) as tif:
        assert tif.is_bigtiff
        assert tif.pages[0].compression == tifffile.COMPRESSION.NONE


# ── deliberately-non-OME variant ───────────────────────────────────────────────
def test_plain_write_is_bigtiff_and_carries_no_ome_header(tmp_path) -> None:
    data = np.zeros((32, 32), np.uint16)
    out = image_io.write_plain_tiff(
        tmp_path / "chan.tiff",
        data,
        why_not_ome="an OME header here would be a second channel-name source",
        compression="zlib",
        resolution=(1e4 / 0.325, 1e4 / 0.325),
        resolutionunit="CENTIMETER",
    )
    with tifffile.TiffFile(out) as tif:
        assert tif.is_bigtiff
        assert not tif.is_ome, (
            "split_multichannel.py and tiled_stitch.py must stay non-OME: an OME "
            "header is a duplicate source of channel names"
        )
        assert tif.pages[0].tags["ResolutionUnit"].value == tifffile.RESUNIT.CENTIMETER


def test_plain_write_requires_a_stated_reason(tmp_path) -> None:
    """Non-OME is a documented variant, not a default. Reaching it without saying why
    is exactly how mechanism 5 (no OME, no bigtiff, no reason) got written five times."""
    with pytest.raises(TypeError):
        image_io.write_plain_tiff(tmp_path / "x.tif", np.zeros((4, 4), np.uint16))
    with pytest.raises(ValueError):
        image_io.write_plain_tiff(
            tmp_path / "x.tif", np.zeros((4, 4), np.uint16), why_not_ome="  "
        )


# ── streamed / pyramid variant ─────────────────────────────────────────────────
def test_open_tiff_writer_streams_a_bigtiff(tmp_path) -> None:
    """TILED_STITCH's shape: a generator of tiles, never a whole array in RAM."""
    out_path = tmp_path / "stitched.tif"
    h = w = 32
    tile = 16

    def tiles():
        for _c in range(2):
            for _y in range(0, h, tile):
                for _x in range(0, w, tile):
                    yield np.full((tile, tile), 3, np.uint16)

    with image_io.open_tiff_writer(out_path, ome=False) as tw:
        tw.write(
            tiles(),
            shape=(2, h, w),
            dtype=np.uint16,
            tile=(tile, tile),
            photometric="minisblack",
        )

    with tifffile.TiffFile(out_path) as tif:
        assert tif.is_bigtiff
        assert not tif.is_ome
    np.testing.assert_array_equal(
        tifffile.imread(out_path), np.full((2, h, w), 3, np.uint16)
    )


def test_open_tiff_writer_ome_pyramid_keeps_subifds(tmp_path) -> None:
    """MERGE_AND_PYRAMID's shape: base level + one subifd level, OME. The owner must
    not flatten the pyramid -- that structure is what consumers read."""
    out_path = tmp_path / "pyr.ome.tif"
    base = np.zeros((2, 32, 32), np.uint16)
    with image_io.open_tiff_writer(out_path, ome=True) as tw:
        tw.write(base, subifds=1, metadata={"axes": "CYX"}, photometric="minisblack")
        tw.write(base[:, ::2, ::2], subfiletype=1, photometric="minisblack")

    with tifffile.TiffFile(out_path) as tif:
        assert tif.is_bigtiff
        assert tif.is_ome
        assert len(tif.series[0].levels) == 2, "the pyramid level was lost"


def test_open_tiff_writer_closes_the_file_on_error(tmp_path) -> None:
    out_path = tmp_path / "boom.tif"
    with pytest.raises(RuntimeError):
        with image_io.open_tiff_writer(out_path, ome=False) as tw:
            tw.write(np.zeros((8, 8), np.uint16))
            raise RuntimeError("boom")
    # A leaked handle would keep the file unreadable on Windows and unflushed here.
    with tifffile.TiffFile(out_path) as tif:
        assert tif.is_bigtiff
