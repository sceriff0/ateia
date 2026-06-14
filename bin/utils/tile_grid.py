"""Pure tile-grid computation, identical to VALIS 1.0.0 NonRigidTileRegistrar.register().

`build_grid` reproduces registration's internal grid bookkeeping (non_rigid_registrars.py:1458-1465)
so REG_PREP can emit a tile manifest whose indices/bboxes match what classic VALIS would compute
for itself. Keeping this in one wrapper means the manifest, REG_TILE, and REG_FINALIZE all share
one source of truth for the grid.
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
