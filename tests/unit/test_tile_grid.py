"""Unit test: bin/utils/tile_grid.build_grid must reproduce VALIS 1.0.0's tile grid exactly.

Runs inside mirage-valis:1.0.0 (needs the `valis` package). The grid that
NonRigidTileRegistrar.register() computes internally is:
    bboxes   = warp_tools.get_grid_bboxes(shape_rc, tile_wh, tile_wh, inclusive=True)
    expanded = [warp_tools.expand_bbox(b, tile_buffer, shape_rc) for b in bboxes]
    n_cols   = len(unique(bboxes[:,0])); n_rows = len(unique(bboxes[:,1]))
build_grid must match this verbatim (registration.py:1458-1465).
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

from tile_grid import build_grid  # noqa: E402
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
