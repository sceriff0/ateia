"""Tiling geometry for the STARE registration method.

A slide is divided into **core** tiles that partition it exactly — every pixel belongs to one
core, with no gaps or overlaps — so the stitch step can place each warped core back without
reconciling seams. Each core is *read* with a surrounding **halo** (clamped to the image), giving
per-tile registration the overlap context it needs. Each core's centre is a control point of the
mesh field, one-to-one with a :class:`mesh_field.MeshField` grid.

Pure geometry — no image data, no third-party deps beyond the standard library.

This module also owns the **tile-plan CSV schema** (:data:`TILE_PLAN_COLUMNS` +
:func:`write_tile_plan`): the artifact TILED_COARSE hands to the Nextflow fan-out and
TILED_REG_TILE reads back as CLI flags. Its Groovy counterpart is ``lib/TilePlan.groovy``
(used by the stub and by the consumer's flag rendering); the two are tied together by
``tests/test_tile_plan_schema.py``. Nobody else may spell the column list out.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass

__all__ = ["Tile", "tile_grid", "TILE_PLAN_COLUMNS", "write_tile_plan"]

# The tile-plan CSV schema. Column names ARE the flag names bin/tiled_reg_tile.py takes,
# so a rename here is a rename of the CLI contract -- see tests/test_tile_plan_schema.py.
TILE_PLAN_COLUMNS = (
    "ix",
    "iy",
    "cx",
    "cy",
    "x0",
    "y0",
    "x1",
    "y1",
    "rx0",
    "ry0",
    "rx1",
    "ry1",
)


@dataclass(frozen=True)
class Tile:
    """One tile of the grid.

    Attributes
    ----------
    ix, iy : int
        Column / row index into the control grid.
    cx, cy : float
        Core centre (the mesh control point), in image pixel coordinates.
    core : tuple(int, int, int, int)
        Owned region ``(x0, y0, x1, y1)``, half-open; cores partition the image.
    read : tuple(int, int, int, int)
        Core expanded by the halo and clamped to the image — the region to load.
    """

    ix: int
    iy: int
    cx: float
    cy: float
    core: tuple
    read: tuple


def _edges(size, tile):
    """Tile boundaries ``0, tile, 2*tile, …, size`` (the last cell may be short)."""
    if tile <= 0:
        raise ValueError(f"tile size must be positive, got {tile}")
    n = (size + tile - 1) // tile  # ceil(size / tile)
    return [min(i * tile, size) for i in range(n + 1)]


def tile_grid(width, height, tile, halo):
    """Build the list of :class:`Tile` covering a ``width`` x ``height`` image."""
    ex = _edges(width, tile)
    ey = _edges(height, tile)
    tiles = []
    for iy in range(len(ey) - 1):
        y0, y1 = ey[iy], ey[iy + 1]
        for ix in range(len(ex) - 1):
            x0, x1 = ex[ix], ex[ix + 1]
            core = (x0, y0, x1, y1)
            read = (
                max(0, x0 - halo),
                max(0, y0 - halo),
                min(width, x1 + halo),
                min(height, y1 + halo),
            )
            tiles.append(
                Tile(
                    ix=ix,
                    iy=iy,
                    cx=(x0 + x1) / 2.0,
                    cy=(y0 + y1) / 2.0,
                    core=core,
                    read=read,
                )
            )
    return tiles


def write_tile_plan(path, tiles):
    """Write ``tiles`` as the tile-plan CSV, header and field order from TILE_PLAN_COLUMNS."""
    with open(path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(list(TILE_PLAN_COLUMNS))
        for t in tiles:
            wr.writerow([t.ix, t.iy, t.cx, t.cy, *t.core, *t.read])
