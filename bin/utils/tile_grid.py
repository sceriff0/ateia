"""Pure tile-grid computation for the two grids the distributed path needs.

`build_grid` is the INPUT grid: identical to VALIS 1.0.0 NonRigidTileRegistrar.register(), it
reproduces registration's internal grid bookkeeping (non_rigid_registrars.py:1458-1465) so REG_PREP
can emit a tile manifest whose indices/bboxes match what classic VALIS would compute for itself.
Keeping this in one wrapper means the manifest, REG_TILE, and REG_FINALIZE all share one source of
truth for the grid.

`output_grid` is the OUTPUT grid, which has no VALIS counterpart: it partitions the warped canvas so
the full-res warp can be fanned out over processes (spec §5.3). It is deliberately buffer-free --
see its docstring.
"""
import numpy as np
from valis import warp_tools


def build_grid(shape_rc, tile_wh=512, tile_buffer=100):
    """Compute the VALIS tile grid for an image of shape `shape_rc` (rows, cols).

    Returns a JSON-serializable dict with the expanded (buffer-grown) tile bboxes and grid
    dimensions. Mirrors NonRigidTileRegistrar.register() exactly:
        temp_tile_bboxes = get_grid_bboxes(shape, tile_wh, tile_wh, inclusive=True)
        expanded_bboxes  = [expand_bbox(b, tile_buffer, shape) for b in temp_tile_bboxes]
        n_cols = len(unique(temp_tile_bboxes[:, 0])); n_rows = len(unique(temp_tile_bboxes[:, 1]))
    """
    shape = np.array(shape_rc)
    bboxes = warp_tools.get_grid_bboxes(shape, tile_wh, tile_wh, inclusive=True)
    expanded = np.array([warp_tools.expand_bbox(b, tile_buffer, shape) for b in bboxes])
    return {
        "shape_rc": [int(shape[0]), int(shape[1])],
        "tile_wh": int(tile_wh),
        "tile_buffer": int(tile_buffer),
        "expanded_bboxes": expanded.tolist(),
        "n_tiles": int(len(bboxes)),
        "n_cols": int(len(np.unique(bboxes[:, 0]))),
        "n_rows": int(len(np.unique(bboxes[:, 1]))),
    }


def output_grid(width, height, tile_wh):
    """Partition an OUTPUT canvas into a regular grid of non-overlapping tiles.

    Unlike ``build_grid``, this grid carries NO buffer/halo, and that is not an approximation:
    each tile is produced as a ``.crop()`` of the SAME lazy pyvips warp, and pyvips is
    demand-driven, so evaluating a cropped region pulls exactly the source pixels that region
    needs -- including the bicubic interpolator's halo, across the tile edge -- computed by the
    same code as the whole-image warp. The tiles are therefore bit-identical to the corresponding
    regions of the single-process warp BY CONSTRUCTION, and can be joined edge-to-edge with no
    blending (verified as leg 2 of tests/integration/verify_lowmem_bitidentical.py).

    Tiles are numbered row-major and cover the canvas exactly: reg_assemble.py selects a row with
    ``t["y"] == r * tile_wh`` and joins in ascending ``x``, so both properties are load-bearing.
    """
    if tile_wh <= 0:
        # range(..., 0) raises an opaque "arg 3 must not be zero" from inside the loop, and a
        # negative step silently yields an EMPTY grid that only fails much later, in reg_assemble.
        raise ValueError(f"tile_wh must be positive, got {tile_wh}")
    if width <= 0 or height <= 0:
        raise ValueError(f"canvas must be non-empty, got {width}x{height}")

    tiles = []
    idx = 0
    for y in range(0, height, tile_wh):
        for x in range(0, width, tile_wh):
            tiles.append({"idx": idx, "x": x, "y": y,
                          "w": min(tile_wh, width - x), "h": min(tile_wh, height - y)})
            idx += 1
    return {"n_cols": (width + tile_wh - 1) // tile_wh,
            "n_rows": (height + tile_wh - 1) // tile_wh,
            "tile_wh": tile_wh, "width": width, "height": height, "tiles": tiles}
