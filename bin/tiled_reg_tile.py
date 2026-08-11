#!/usr/bin/env python3
"""STARE fan-out step 2/4: one tile's residual (the embarrassingly-parallel part).

Given the global M0 and a tile's read box, rigid-warps just that reference-frame window of the
moving DAPI, phase-correlates it against the reference window, and emits the tile's control-point
displacement + local TRE. One task per tile — this is the little-process fan-out, so unlike
tiled_stitch.py (one process for the whole slide) this runs N times over. It reads through the
same lazy zarr-region primitives (``open_lazy`` + ``source_region``) tiled_stitch.py uses, so each
invocation decodes only the reference tile and the small moving crop the tile's inverse map draws
from — never the whole slide.
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
from tiled_io import open_lazy  # noqa: E402
from tiled_warp import source_region, warp_image  # noqa: E402

logger = get_logger(__name__)


def _check_nuclear_index(index, c_n):
    """Same out-of-range check + message ``tiled_io.nuclear_channel`` raised, for a lazy source."""
    if not 0 <= index < c_n:
        raise ValueError(f"--nuclear-index {index} out of range for C={c_n}")


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="STARE per-tile residual.")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--moving", required=True)
    ap.add_argument("--m0", required=True, help="M0 JSON from tiled_coarse")
    ap.add_argument(
        "--nuclear-index",
        "--dapi-index",  # deprecated alias, kept so hand-run commands keep working
        dest="nuclear_index",
        type=int,
        default=0,
        help=(
            "Index of the nuclear/fiducial channel the transform is estimated from. "
            "The pipeline resolves this from channel metadata (MarkerUtils) and passes "
            "it explicitly; 0 is CONVERT_IMAGE's promoted position."
        ),
    )
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

    # Lazy zarr-region reads (the tiled_stitch.py pattern): decode only the reference tile and
    # the small moving crop the tile's inverse map draws from, never either whole slide. The
    # acquisitions are nested (not two opens followed by one try/finally) so that if opening the
    # moving slide raises, the already-open reference handle is still closed.
    ref_src, _ref_dtype, ref_close = open_lazy(a.reference)
    try:
        mov_src, _mov_dtype, mov_close = open_lazy(a.moving)
        try:
            _check_nuclear_index(a.nuclear_index, ref_src.shape[0])
            _check_nuclear_index(a.nuclear_index, mov_src.shape[0])
            out_h, out_w = a.ry1 - a.ry0, a.rx1 - a.rx0
            ref_tile = np.asarray(
                ref_src[a.nuclear_index, slice(a.ry0, a.ry1), slice(a.rx0, a.rx1)],
                dtype=np.float32,
            )
            _c, mh, mw = mov_src.shape
            sx0, sy0, sx1, sy1 = source_region(
                m0, None, (a.rx0, a.ry0), (out_h, out_w), src_shape=(mh, mw)
            )
            if sx1 > sx0 and sy1 > sy0:
                crop = np.asarray(
                    mov_src[a.nuclear_index, slice(sy0, sy1), slice(sx0, sx1)],
                    dtype=float,
                )
                mov_tile = warp_image(
                    crop,
                    m0,
                    None,
                    (out_h, out_w),
                    out_origin=(a.rx0, a.ry0),
                    src_origin=(sx0, sy0),
                )
            else:
                mov_tile = np.zeros((out_h, out_w), dtype=float)
        finally:
            mov_close()
    finally:
        ref_close()

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
