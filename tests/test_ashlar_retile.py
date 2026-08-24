"""Tests for bin/ashlar_retile.py — stitched WSI -> the tile grid ASHLAR's reader accepts.

ASHLAR never reads tile positions from a file; ``FilePatternMetadata.tile_position`` COMPUTES
them from ``{row}``/``{col}`` and the grid constants. Two consequences are load-bearing and
both are pinned here:

* the grid must reproduce ``[row, col] * tile_size * (1 - overlap)`` exactly, and
* every tile must be the SAME size, because ``tile_size(i)`` is read from the first tile and
  applied to all of them. The previous implementation of this module cropped edge tiles
  ("never padded"), which the reader cannot consume.

``test_grid_satisfies_ashlars_own_reader_validation`` replays ``_enumerate_tiles``' regex and
its rectangular-grid check against the real output directory, so the format is verified
without ashlar or a container.
"""

from __future__ import annotations

import json
import re

import numpy as np
import pytest
from ashlar_retile import (
    DEFAULT_PIXEL_SIZE_UM,
    TILE_PATTERN,
    grid_shape,
    tile_grid,
    write_tiles,
)

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("zarr")


def _write_ome(path, arr, pixel_size=None):
    metadata = {"axes": "CYX"}
    if pixel_size is not None:
        metadata["PhysicalSizeX"] = pixel_size
        metadata["PhysicalSizeXUnit"] = "um"
    tifffile.imwrite(path, arr, photometric="minisblack", metadata=metadata)


def _source(tmp_path, shape=(2, 512, 640), pixel_size=None, seed=0):
    img = np.random.default_rng(seed).integers(0, 255, size=shape, dtype=np.uint8)
    src = tmp_path / "cycle0.ome.tif"
    _write_ome(src, img, pixel_size)
    return src, img


# --- grid maths -------------------------------------------------------------------------

def test_grid_reproduces_ashlars_tile_position_formula():
    """The whole reason fabricated positions are exact: same arithmetic, both sides."""
    tile_size, overlap = 400, 0.25
    for t in tile_grid(1000, 1000, tile_size, overlap):
        assert t.x == t.col * tile_size * (1 - overlap)
        assert t.y == t.row * tile_size * (1 - overlap)


def test_grid_covers_every_pixel_column():
    tiles = tile_grid(1000, 1000, 400, 0.25)
    assert grid_shape(1000, 1000, 400, 0.25) == (4, 4)
    covered = set()
    for t in tiles:
        covered.update(range(t.x, t.x + t.w))
    assert covered == set(range(1000))


def test_grid_is_a_full_rectangle_even_when_edge_cells_are_slivers():
    """ashlar raises "Tiles do not form a full rectangular grid" on a ragged grid."""
    n_rows, n_cols = grid_shape(1050, 810, 512, 0.1)
    tiles = tile_grid(1050, 810, 512, 0.1)
    assert len(tiles) == n_rows * n_cols
    assert {(t.row, t.col) for t in tiles} == {
        (r, c) for r in range(n_rows) for c in range(n_cols)
    }


# --- the padding regression -------------------------------------------------------------

def test_every_written_tile_is_exactly_tile_size_square(tmp_path):
    """THE regression. ashlar reads tile_size from the FIRST tile and applies it to all.

    A cropped edge tile is declared full-size and read back short, and the mismatch
    surfaces inside EdgeAligner rather than here.
    """
    src, _ = _source(tmp_path, shape=(2, 500, 700))
    outdir = tmp_path / "tiles"
    write_tiles(src, outdir, tile_size=256, overlap_fraction=0.1)

    written = sorted(outdir.glob("r*_c*.tif"))
    assert written
    for path in written:
        assert tifffile.imread(path).shape == (2, 256, 256), path.name


def test_padding_is_zero_and_valid_extent_says_where_it_starts(tmp_path):
    """Padding must be identifiable, or downstream reads it as signal."""
    src, img = _source(tmp_path, shape=(1, 300, 300), seed=3)
    outdir = tmp_path / "tiles"
    grid = json.loads(write_tiles(src, outdir, tile_size=256, overlap_fraction=0.0).read_text())

    extent = {(r, c): (w, h) for r, c, w, h in grid["valid_extent"]}
    assert extent[(1, 1)] == (44, 44)                     # 300 - 256
    corner = tifffile.imread(outdir / TILE_PATTERN.format(row=1, col=1))
    np.testing.assert_array_equal(corner[0, :44, :44], img[0, 256:300, 256:300])
    assert not corner[0, 44:, :].any() and not corner[0, :, 44:].any()


def test_tiles_reconstruct_the_source_from_their_valid_extents(tmp_path):
    src, img = _source(tmp_path, shape=(2, 512, 640), seed=1)
    outdir = tmp_path / "tiles"
    grid = json.loads(write_tiles(src, outdir, tile_size=256, overlap_fraction=0.25).read_text())

    recon = np.zeros(img.shape[1:], dtype=img.dtype)
    stride = grid["stride"]
    for row, col, w, h in grid["valid_extent"]:
        tile = tifffile.imread(outdir / TILE_PATTERN.format(row=row, col=col))
        y, x = row * stride, col * stride
        recon[y : y + h, x : x + w] = tile[0, :h, :w]
    np.testing.assert_array_equal(recon, img[0])


def test_all_channels_are_kept_so_the_nuclear_index_is_stable(tmp_path):
    """The pattern carries no {channel} field, which selects ashlar's multi_channel_tiles
    branch — it re-reads the channel map off axis 0, so channel identity must survive."""
    src, img = _source(tmp_path, shape=(3, 128, 128), seed=2)
    outdir = tmp_path / "tiles"
    write_tiles(src, outdir, tile_size=128, overlap_fraction=0.0)
    tile = tifffile.imread(outdir / TILE_PATTERN.format(row=0, col=0))
    assert tile.shape[0] == 3
    for c in range(3):
        np.testing.assert_array_equal(tile[c], img[c])


# --- the format ashlar actually parses ---------------------------------------------------

def test_grid_satisfies_ashlars_own_reader_validation(tmp_path):
    """Replay FilePatternMetadata._enumerate_tiles against the real output directory."""
    src, _ = _source(tmp_path, shape=(2, 700, 900))
    outdir = tmp_path / "tiles"
    grid = json.loads(write_tiles(src, outdir, tile_size=256, overlap_fraction=0.1).read_text())

    pattern = TILE_PATTERN.replace(".", r"\.").replace("(", r"\(").replace(")", r"\)")
    regex = re.sub(r"{([^:}]+)(?:[^}]*)}", r"(?P<\1>.*?)", pattern)

    rows, cols, channels, n = set(), set(), set(), 0
    for p in outdir.iterdir():
        match = re.match(regex, p.name)
        if match:
            gd = match.groupdict()
            rows.add(int(gd["row"]))
            cols.add(int(gd["col"]))
            channels.add(gd.get("channel"))
            n += 1
    assert n == len(rows) * len(cols) * len(channels), "not a full rectangular grid"
    assert (len(rows), len(cols)) == (grid["n_rows"], grid["n_cols"])


def test_no_positions_csv_is_written(tmp_path):
    """ashlar has no reader that consumes one; the prior implementation wrote it anyway."""
    src, _ = _source(tmp_path, shape=(1, 128, 128))
    outdir = tmp_path / "tiles"
    write_tiles(src, outdir, tile_size=64, overlap_fraction=0.0)
    assert not list(outdir.glob("*.csv"))
    assert (outdir / "grid.json").exists()


# --- pixel size, which silently rescales --maximum-shift ---------------------------------

def test_physical_size_x_is_read_when_present(tmp_path):
    src, _ = _source(tmp_path, shape=(2, 64, 64), pixel_size=0.5)
    grid = json.loads(
        write_tiles(src, tmp_path / "t", tile_size=64, overlap_fraction=0.0).read_text())
    assert grid["pixel_size_um"] == 0.5


def test_pixel_size_falls_back_when_metadata_is_absent(tmp_path):
    src, _ = _source(tmp_path, shape=(2, 64, 64))
    grid = json.loads(
        write_tiles(src, tmp_path / "t", tile_size=64, overlap_fraction=0.0).read_text())
    assert grid["pixel_size_um"] == DEFAULT_PIXEL_SIZE_UM
