#!/usr/bin/env python3
"""STARE fan-out step 1/4: global rigid anchor (M0) + tile plan.

Estimates the whole-slide rigid ``M0`` (moving -> reference) from the DAPI channels and writes the
tile grid the downstream per-tile registration fans out over. One cheap task per moving slide.

The estimate runs on a **thumbnail**, as docs/parallel_registration_design.md always specified.
Both slides are read lazily (zarr region reads, in row bands) and decimated by one shared integer
factor, so peak memory is a band plus the thumbnail rather than the full-resolution plane. This
matters more than it looks: scikit-image's ORB promotes its input to float64 and builds a
Gaussian pyramid plus FAST/Harris response arrays on top, which measures at roughly 40 bytes per
source pixel -- around 35 GB on a 26k x 26k slide if handed the native-resolution plane.

``--max-dim`` is the accuracy/cost knob. The anchor only has to land the moving slide inside the
per-tile read halo (``--halo``, reference-frame px); the per-tile step refines the residual from
there. A coarse fit residual of ``r`` thumbnail px becomes ``r * factor`` full-res px, so keep
``max_dim`` large enough that ``factor`` stays comfortably under ``halo``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from coarse_align import estimate_rigid, scale_transform_to_full_res  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tile_grid import tile_grid  # noqa: E402
from tiled_io import decimation_factor, open_lazy, read_decimated  # noqa: E402

logger = get_logger(__name__)


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="STARE coarse anchor + tile plan.")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--moving", required=True)
    ap.add_argument("--dapi-index", type=int, default=0)
    ap.add_argument("--tile", type=int, default=2048)
    ap.add_argument("--halo", type=int, default=256)
    ap.add_argument(
        "--max-dim",
        type=int,
        default=4096,
        help="longest thumbnail side (px) the anchor is estimated on; <=0 disables decimation",
    )
    ap.add_argument(
        "--model", default="euclidean", choices=["euclidean", "similarity", "affine"]
    )
    ap.add_argument("--out-m0", required=True, help="output M0 JSON (+ reference dims)")
    ap.add_argument("--out-tiles", required=True, help="output tile-plan CSV")
    a = ap.parse_args(argv)

    # Nested acquisition (not two opens then one try/finally) so a failure opening the moving
    # slide still closes the already-open reference handle -- the tiled_reg_tile.py pattern.
    ref_src, _ref_dtype, ref_close = open_lazy(a.reference)
    try:
        mov_src, _mov_dtype, mov_close = open_lazy(a.moving)
        try:
            _c, h, w = ref_src.shape  # FULL-resolution reference dims: the tile plan's frame
            _mc, mh, mw = mov_src.shape
            # ONE factor for both slides: ORB matches descriptors across the two thumbnails, so
            # a per-slide factor would introduce a scale change M0 cannot represent honestly.
            factor = decimation_factor([(h, w), (mh, mw)], a.max_dim)
            ref_dapi = read_decimated(ref_src, a.dapi_index, factor)
            mov_dapi = read_decimated(mov_src, a.dapi_index, factor)
        finally:
            mov_close()
    finally:
        ref_close()

    m0_ds, coarse_tre_ds, n_inliers = estimate_rigid(ref_dapi, mov_dapi, model=a.model)
    # The fit lives in thumbnail pixels; everything downstream (tile plan, per-tile source
    # regions, the stitch) is full-resolution, so lift both the map and its residual here.
    m0 = scale_transform_to_full_res(m0_ds, factor)
    coarse_tre = float(coarse_tre_ds) * factor

    Path(a.out_m0).write_text(
        json.dumps(
            {
                "M0": m0.tolist(),
                "ref_h": int(h),
                "ref_w": int(w),
                "ref_name": Path(a.reference).stem,
                "coarse_tre": float(coarse_tre),
                "n_inliers": int(n_inliers),
                "coarse_factor": int(factor),
            },
            indent=2,
        )
    )

    tiles = tile_grid(w, h, a.tile, a.halo)
    with open(a.out_tiles, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(
            ["ix", "iy", "cx", "cy", "x0", "y0", "x1", "y1", "rx0", "ry0", "rx1", "ry1"]
        )
        for t in tiles:
            wr.writerow([t.ix, t.iy, t.cx, t.cy, *t.core, *t.read])

    logger.info(
        f"coarse: M0 (n_inliers={n_inliers}, tre={coarse_tre:.2f}px) from a "
        f"{ref_dapi.shape[1]}x{ref_dapi.shape[0]} thumbnail (1/{factor}), "
        f"{len(tiles)} tiles ({a.tile}px+{a.halo} halo) over {w}x{h}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
