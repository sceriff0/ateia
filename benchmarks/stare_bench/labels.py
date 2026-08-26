"""Which tiles were TRULY registrable -- the gate-ROC ground truth.

Computed ENTIRELY from the generator's own state. There is deliberately no
parameter through which a registration result could enter: a label derived from
what a method did would make the gate ROC a measurement of itself.

A tile is registrable iff
  (a) it lands on-section in both frames after the ground-truth warp, and
  (b) its post-physics moving crop retains at least TAU of its pre-physics
      signal energy.

TAU is chosen on the generator side. It is CHECKED against this repo's measured
mov_fg/ref_fg >= 0.4165 separator (bin/tiled_reg_tile.py) for consistency, and
never fitted to it.
"""

from __future__ import annotations

import numpy as np

__all__ = ["TAU", "tile_labels"]

TAU = 0.35


def tile_labels(retained, shape, tile, field, tau=TAU):
    """One label record per tile of a ``shape`` raster at ``tile`` spacing."""
    retained = np.asarray(retained, dtype=np.float32)
    h, w = int(shape[0]), int(shape[1])
    tile = int(tile)
    out = []
    for iy, y0 in enumerate(range(0, h, tile)):
        for ix, x0 in enumerate(range(0, w, tile)):
            y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
            cy, cx = (y0 + y1) / 2.0, (x0 + x1) / 2.0
            block = retained[y0:y1, x0:x1]
            retained_mean = float(block.mean()) if block.size else 0.0

            disp = field.sample(np.array([[cx, cy]]))[0]
            wx, wy = cx + float(disp[0]), cy + float(disp[1])
            on_section = bool(0 <= wx < w and 0 <= wy < h)

            out.append({
                "ix": ix,
                "iy": iy,
                "cx": cx,
                "cy": cy,
                "retained_mean": retained_mean,
                "on_section": on_section,
                "registrable": bool(on_section and retained_mean >= tau),
            })
    return out
