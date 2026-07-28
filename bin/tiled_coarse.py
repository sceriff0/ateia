#!/usr/bin/env python3
"""STARE fan-out step 1/4: global rigid anchor (M0) + tile plan.

Estimates the whole-slide rigid ``M0`` (moving -> reference) from the DAPI channels and writes the
tile grid the downstream per-tile registration fans out over. One cheap task per moving slide.
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

from coarse_align import estimate_rigid  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tile_grid import tile_grid  # noqa: E402
from tiled_io import dapi_channel, load_channels  # noqa: E402

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
        "--model", default="euclidean", choices=["euclidean", "similarity", "affine"]
    )
    ap.add_argument("--out-m0", required=True, help="output M0 JSON (+ reference dims)")
    ap.add_argument("--out-tiles", required=True, help="output tile-plan CSV")
    a = ap.parse_args(argv)

    ref_dapi = dapi_channel(load_channels(a.reference), a.dapi_index)
    mov_dapi = dapi_channel(load_channels(a.moving), a.dapi_index)
    h, w = ref_dapi.shape

    m0, coarse_tre, n_inliers = estimate_rigid(ref_dapi, mov_dapi, model=a.model)
    Path(a.out_m0).write_text(
        json.dumps(
            {
                "M0": m0.tolist(),
                "ref_h": int(h),
                "ref_w": int(w),
                "ref_name": Path(a.reference).stem,
                "coarse_tre": float(coarse_tre),
                "n_inliers": int(n_inliers),
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
        f"coarse: M0 (n_inliers={n_inliers}, tre={coarse_tre:.2f}px), "
        f"{len(tiles)} tiles ({a.tile}px+{a.halo} halo) over {w}x{h}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
