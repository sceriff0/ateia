"""Unit tests for bin/utils/tile_grid.

`build_grid` must reproduce VALIS 1.0.0's INPUT tile grid exactly. Runs inside
mirage-valis:1.0.0 (needs the `valis` package). The grid that
NonRigidTileRegistrar.register() computes internally is:
    bboxes   = warp_tools.get_grid_bboxes(shape_rc, tile_wh, tile_wh, inclusive=True)
    expanded = [warp_tools.expand_bbox(b, tile_buffer, shape_rc) for b in bboxes]
    n_cols   = len(unique(bboxes[:,0])); n_rows = len(unique(bboxes[:,1]))
build_grid must match this verbatim (registration.py:1458-1465).

`output_grid` is a DIFFERENT grid with no VALIS counterpart: it partitions the OUTPUT
canvas of the full-res warp so each region can be warped by a separate process. Its
contract is exact tiling -- non-overlapping, gapless, and covering the canvas exactly --
because reg_assemble.py joins the tiles back edge-to-edge with no blending. Any overlap
or gap here is a visible seam or a shifted slide, so the tests below assert coverage by
reconstruction, not just by counting tiles.
"""
import os
import sys

import numpy as np

# Needs the valis image (pyvips + valis); skip under a plain python env (CI Python job).
try:
    import pytest
    pytest.importorskip("pyvips")
    pytest.importorskip("valis")
except ImportError:  # stdlib __main__ runner in-image (not under pytest)
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bin", "utils"))

from tile_grid import build_grid, output_grid  # noqa: E402
from valis import warp_tools  # noqa: E402  (reference 1.0.0 in the image)


def _reference(shape_rc, tile_wh, tile_buffer):
    shape = np.array(shape_rc)
    bboxes = warp_tools.get_grid_bboxes(shape, tile_wh, tile_wh, inclusive=True)
    expanded = np.array([warp_tools.expand_bbox(b, tile_buffer, shape) for b in bboxes])
    return bboxes, expanded


def test_grid_matches_valis_exactly():
    shape_rc = (1700, 1300)
    tile_wh, tile_buffer = 512, 100
    grid = build_grid(shape_rc, tile_wh, tile_buffer)
    bboxes, expanded = _reference(shape_rc, tile_wh, tile_buffer)

    assert np.array_equal(np.array(grid["expanded_bboxes"]), expanded)
    assert grid["n_tiles"] == len(bboxes)
    assert grid["n_cols"] == len(np.unique(bboxes[:, 0]))
    assert grid["n_rows"] == len(np.unique(bboxes[:, 1]))
    assert grid["tile_wh"] == tile_wh
    assert grid["tile_buffer"] == tile_buffer
    assert grid["shape_rc"] == [1700, 1300]


def test_grid_multi_tile_small_shape():
    # mirrors the spike's 128px / tile_wh=48 multi-tile regime
    grid = build_grid((128, 128), 48, 16)
    bboxes, expanded = _reference((128, 128), 48, 16)
    assert grid["n_tiles"] == len(bboxes) >= 4
    assert np.array_equal(np.array(grid["expanded_bboxes"]), expanded)


def test_grid_json_serializable():
    import json
    grid = build_grid((600, 400), 256, 32)
    # every value must survive json round-trip (manifest is shipped as JSON)
    assert json.loads(json.dumps(grid)) == grid


def _assert_exact_cover(grid, width, height):
    """Every output pixel is claimed by exactly one tile -- no overlap, no gap, no overrun.

    Painted as a coverage counter rather than compared bbox-by-bbox: a counter catches
    double-cover and under-cover the same way, whatever the tiling order.
    """
    cover = np.zeros((height, width), dtype=np.int32)
    for t in grid["tiles"]:
        assert t["w"] > 0 and t["h"] > 0, f"degenerate tile {t}"
        cover[t["y"]:t["y"] + t["h"], t["x"]:t["x"] + t["w"]] += 1
    assert cover.min() == 1 and cover.max() == 1, (
        f"tiles do not exactly cover {width}x{height}: "
        f"min cover={cover.min()} max cover={cover.max()}")


def test_output_grid_exactly_covers_ragged_canvas():
    # 300x220 with tile_wh=128 -> the right column and bottom row are BOTH short (44 / 92),
    # the case where an off-by-one silently drops or duplicates a strip of the slide.
    w, h, tw = 300, 220, 128
    grid = output_grid(w, h, tw)
    assert grid["width"] == w and grid["height"] == h and grid["tile_wh"] == tw
    assert grid["n_cols"] == 3 and grid["n_rows"] == 2
    assert len(grid["tiles"]) == grid["n_cols"] * grid["n_rows"]
    _assert_exact_cover(grid, w, h)


def test_output_grid_exact_multiple_has_no_short_tiles():
    grid = output_grid(256, 512, 128)
    assert grid["n_cols"] == 2 and grid["n_rows"] == 4
    assert all(t["w"] == 128 and t["h"] == 128 for t in grid["tiles"])
    _assert_exact_cover(grid, 256, 512)


def test_output_grid_smaller_than_one_tile():
    # reg_assemble clamps --tile-wh nowhere, so a canvas smaller than the tile must still
    # yield one full-canvas tile rather than an empty or oversized region.
    grid = output_grid(50, 30, 4096)
    assert grid["n_cols"] == 1 and grid["n_rows"] == 1
    assert grid["tiles"] == [{"idx": 0, "x": 0, "y": 0, "w": 50, "h": 30}]
    _assert_exact_cover(grid, 50, 30)


def test_output_grid_indices_are_row_major_and_dense():
    # reg_warp_tile is dispatched by --tile-idx and reg_assemble selects a row with
    # `t["y"] == r * tile_wh`, so indices must be dense and rows must land on tile_wh
    # multiples. A sparse or column-major numbering would assemble a scrambled slide.
    grid = output_grid(300, 220, 128)
    assert [t["idx"] for t in grid["tiles"]] == list(range(len(grid["tiles"])))
    for r in range(grid["n_rows"]):
        row = [t for t in grid["tiles"] if t["y"] == r * grid["tile_wh"]]
        assert len(row) == grid["n_cols"]
        assert [t["x"] for t in row] == sorted(t["x"] for t in row)


def test_output_grid_rejects_degenerate_arguments():
    # Each of these used to fail late and obscurely: tile_wh=0 raised from inside range(), and a
    # negative step produced an EMPTY grid that only blew up later in reg_assemble.
    for bad in ((100, 100, 0), (100, 100, -64), (0, 100, 64), (100, -1, 64)):
        try:
            output_grid(*bad)
        except ValueError:
            continue
        raise AssertionError(f"output_grid{bad} should have raised ValueError")


def test_output_grid_json_serializable():
    import json
    grid = output_grid(300, 220, 128)
    assert json.loads(json.dumps(grid)) == grid


if __name__ == "__main__":
    # stdlib runner so this works in the VALIS image without pytest installed
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
