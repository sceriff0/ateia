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

Raising it is not linear, though, and that asymmetry is the thing to know before tuning: ORB's
cost is dominated by ``corner_peaks``, which is quadratic in the number of candidate corners, so
cost grows roughly with the SQUARE of the pixel count. Measured on a realistic DAPI phantom,
per slide: 2048 -> ~13 s, 4096 -> ~154 s. 8192 would be tens of minutes per slide, and two
slides run per task. Lower ``--max-dim`` when the step is slow; raise it only if the logged
residual approaches ``--halo``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
from coarse_align import estimate_rigid, scale_transform_to_full_res  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tile_grid import tile_grid  # noqa: E402
from tiled_io import (  # noqa: E402
    band_rows_for,
    decimation_factor,
    open_lazy,
    read_decimated,
)

logger = get_logger(__name__)


def _size_gb(path) -> float:
    try:
        return os.path.getsize(path) / 1e9
    except OSError:
        return float("nan")


def _timed_read(src, index, factor, which):
    """Read one decimated DAPI plane, logging how long it took and what came back.

    The intensity summary is not decoration. ORB's `fast_threshold` is an ABSOLUTE intensity
    difference, so a plane arriving in raw uint16 counts rather than [0, 1] silently costs
    ~100x in the anchor step (see the note in bin/utils/coarse_align.py). Printing the observed
    range here is what makes that class of regression visible in `.command.out` instead of
    presenting as a walltime kill with no output at all.
    """
    t0 = time.perf_counter()
    plane = read_decimated(src, index, factor)
    lo, med, hi = (float(v) for v in np.percentile(plane, (1.0, 50.0, 99.9)))
    logger.info(
        f"coarse: read {which} DAPI in {time.perf_counter() - t0:.1f}s -> "
        f"{plane.shape[1]}x{plane.shape[0]} {plane.dtype} "
        f"({plane.nbytes / 1e6:.0f} MB), intensity p1/p50/p99.9 = "
        f"{lo:.1f}/{med:.1f}/{hi:.1f}"
    )
    return plane


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="STARE coarse anchor + tile plan.")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--moving", required=True)
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

    t_start = time.perf_counter()
    logger.info(
        f"coarse: start | reference={Path(a.reference).name} "
        f"({_size_gb(a.reference):.2f} GB) moving={Path(a.moving).name} "
        f"({_size_gb(a.moving):.2f} GB) | nuclear_index={a.nuclear_index} model={a.model} "
        f"max_dim={a.max_dim} tile={a.tile} halo={a.halo}"
    )

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
            logger.info(
                f"coarse: reference {w}x{h} C={_c} {_ref_dtype} | "
                f"moving {mw}x{mh} C={_mc} {_mov_dtype} | "
                f"decimation 1/{factor} -> ~{w // factor}x{h // factor} thumbnail, "
                f"streamed in {band_rows_for(w, factor)}-row bands"
            )
            # `not 0 <= i < C`, not `i >= C`: a negative index passes the upper-bound
            # test and then silently reads the LAST channel via Python's wraparound.
            if not 0 <= a.nuclear_index < min(_c, _mc):
                # read_decimated raises on this, but naming BOTH channel counts up front turns
                # "index out of range for C=3" into an actionable message.
                logger.warning(
                    f"coarse: --nuclear-index {a.nuclear_index} is out of range for "
                    f"reference C={_c} / moving C={_mc}"
                )
            ref_nuc = _timed_read(ref_src, a.nuclear_index, factor, "reference")
            mov_nuc = _timed_read(mov_src, a.nuclear_index, factor, "moving")
        finally:
            mov_close()
    finally:
        ref_close()

    t0 = time.perf_counter()
    m0_ds, coarse_tre_ds, n_inliers = estimate_rigid(ref_nuc, mov_nuc, model=a.model)
    logger.info(f"coarse: anchor estimated in {time.perf_counter() - t0:.1f}s")
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

    logger.info(
        f"coarse: M0 dx={m0[0, 2]:+.1f}px dy={m0[1, 2]:+.1f}px "
        f"rot={np.degrees(np.arctan2(m0[1, 0], m0[0, 0])):+.2f}deg | "
        f"n_inliers={n_inliers} residual={coarse_tre:.2f}px full-res "
        f"({coarse_tre_ds:.2f} thumbnail px x {factor})"
    )
    # The anchor's only job is to land the moving slide inside the per-tile read halo; the
    # per-tile step refines from there. Nothing downstream checks that, and a residual wider
    # than the halo does not fail -- it silently produces tiles whose true match lies outside
    # the region that was read, i.e. a mis-registered slide that still exits 0.
    if not np.isfinite(coarse_tre):
        logger.warning("coarse: residual not finite -- the anchor fit did not converge")
    elif coarse_tre >= a.halo:
        logger.warning(
            f"coarse: residual {coarse_tre:.1f}px >= halo {a.halo}px -- the per-tile step may "
            f"not recover this. Raise --halo (reg_tiled_halo) or --max-dim "
            f"(reg_tiled_coarse_max_dim, currently decimating 1/{factor})"
        )
    if n_inliers < 10:
        logger.warning(
            f"coarse: only {n_inliers} inliers support M0 -- treat this slide's anchor as "
            f"unverified (low tissue texture, wrong --nuclear-index, or a failed match)"
        )

    tiles = tile_grid(w, h, a.tile, a.halo)
    with open(a.out_tiles, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(
            ["ix", "iy", "cx", "cy", "x0", "y0", "x1", "y1", "rx0", "ry0", "rx1", "ry1"]
        )
        for t in tiles:
            wr.writerow([t.ix, t.iy, t.cx, t.cy, *t.core, *t.read])

    # The tile count IS the downstream fan-out width: TILED_REG_TILE runs once per row here,
    # so this number is what a reader needs to size the rest of the patient's registration.
    logger.info(
        f"coarse: done in {time.perf_counter() - t_start:.1f}s | "
        f"{len(tiles)} tiles ({a.tile}px core + {a.halo}px halo) over {w}x{h} "
        f"-> {len(tiles)} TILED_REG_TILE tasks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
