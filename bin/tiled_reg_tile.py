#!/usr/bin/env python3
"""STARE fan-out step 2/4: one tile's residual (the embarrassingly-parallel part).

Given the global M0 and a tile's read box, rigid-warps just that reference-frame window of the
moving DAPI, phase-correlates it against the reference window, and emits the tile's control-point
displacement + local TRE. One task per tile — this is the little-process fan-out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tile_residual import residual_displacement  # noqa: E402
from tiled_io import dapi_channel, load_channels  # noqa: E402
from tiled_warp import warp_image  # noqa: E402

logger = get_logger(__name__)


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="STARE per-tile residual.")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--moving", required=True)
    ap.add_argument("--m0", required=True, help="M0 JSON from tiled_coarse")
    ap.add_argument("--dapi-index", type=int, default=0)
    ap.add_argument("--ix", type=int, required=True)
    ap.add_argument("--iy", type=int, required=True)
    ap.add_argument("--cx", type=float, required=True)
    ap.add_argument("--cy", type=float, required=True)
    ap.add_argument("--rx0", type=int, required=True)
    ap.add_argument("--ry0", type=int, required=True)
    ap.add_argument("--rx1", type=int, required=True)
    ap.add_argument("--ry1", type=int, required=True)
    ap.add_argument("--upsample", type=int, default=10)
    ap.add_argument("--out", required=True, help="output control-point JSON")
    a = ap.parse_args(argv)

    m0 = np.asarray(json.loads(Path(a.m0).read_text())["M0"], dtype=float)
    ref_dapi = dapi_channel(load_channels(a.reference), a.dapi_index)
    mov_dapi = dapi_channel(load_channels(a.moving), a.dapi_index)

    ref_tile = ref_dapi[a.ry0 : a.ry1, a.rx0 : a.rx1]
    # rigid-warp only this reference-frame window of the moving slide (memory = one tile)
    mov_tile = warp_image(
        mov_dapi, m0, None, (a.ry1 - a.ry0, a.rx1 - a.rx0), out_origin=(a.rx0, a.ry0)
    )
    dx, dy, tre = residual_displacement(ref_tile, mov_tile, upsample=a.upsample)

    Path(a.out).write_text(
        json.dumps(
            {
                "ix": a.ix,
                "iy": a.iy,
                "cx": a.cx,
                "cy": a.cy,
                "dx": dx,
                "dy": dy,
                "tre": tre,
            }
        )
    )
    logger.info(f"tile ({a.ix},{a.iy}): dxy=({dx:.2f},{dy:.2f}) tre={tre:.2f}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
