#!/usr/bin/env python3
"""STARE fan-out step 4/4: stream the moving slide through the manifest into the registered slide.

Gigapixel-safe: neither the whole moving slide nor the whole output is held in memory. For each
output tile it reads only the moving pixels that tile draws from (``source_region`` + a lazy zarr
region read), warps just that tile (bilinear, non-negative), and writes it straight to a tiled
OME-TIFF. Peak memory is one source crop + one output tile, per channel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
import tifffile  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from mesh_field import MeshField  # noqa: E402
from tiled_io import open_lazy  # noqa: E402
from tiled_warp import source_region, warp_image  # noqa: E402

logger = get_logger(__name__)


def _entry_for(manifest, moving_name):
    slides = manifest["slides"]
    if moving_name and moving_name in slides:
        return moving_name, slides[moving_name]
    movers = [k for k in slides if k != manifest["ref_slide"]]
    if not movers:
        raise ValueError("manifest carries no moving slide")
    return movers[0], slides[movers[0]]


def _mesh_and_margin(entry):
    if entry.get("mesh") is None:
        return None, 4
    m = entry["mesh"]
    disp = np.asarray(m["displacements"], dtype=float)
    mesh = MeshField(m["grid_x"], m["grid_y"], disp)
    # the source box must cover the residual displacement the mesh can add, plus a bilinear pixel
    margin = int(np.ceil(np.abs(disp).max())) + 4
    return mesh, margin


def _clamp(arr, dtype):
    out = np.clip(arr, 0.0, None)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        out = np.clip(np.rint(out), info.min, info.max)
    return out.astype(dtype)


def stream_tiles(src, m0, mesh, margin, out_h, out_w, tile, dtype):
    """Yield tiled output in tifffile order (channel, row, col), warping one tile at a time."""
    c_n, h, w = src.shape
    for c in range(c_n):
        for ty in range(0, out_h, tile):
            for tx in range(0, out_w, tile):
                th, tw = min(tile, out_h - ty), min(tile, out_w - tx)
                out_tile = np.zeros((tile, tile), dtype=dtype)
                sx0, sy0, sx1, sy1 = source_region(
                    m0, mesh, (tx, ty), (th, tw), margin=margin, src_shape=(h, w)
                )
                if sx1 > sx0 and sy1 > sy0:
                    crop = np.asarray(
                        src[c, slice(sy0, sy1), slice(sx0, sx1)], dtype=float
                    )
                    warped = warp_image(
                        crop,
                        m0,
                        mesh,
                        (th, tw),
                        out_origin=(tx, ty),
                        src_origin=(sx0, sy0),
                    )
                    out_tile[:th, :tw] = _clamp(warped, dtype)
                yield out_tile


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="STARE streaming stitch via the manifest.")
    ap.add_argument("--moving", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--moving-name", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--out-tile", type=int, default=1024, help="output write-tile size (px)"
    )
    ap.add_argument(
        "--pixel-size",
        dest="pixel_size",
        type=float,
        default=0.325,
        help="Configured scale in µm/px (TILED_STITCH passes params.pixel_size), "
        "stamped on the stitched slide as resolution tags.",
    )
    a = ap.parse_args(argv)

    manifest = json.loads(Path(a.manifest).read_text())
    name, entry = _entry_for(manifest, a.moving_name)
    m0 = np.asarray(entry["M0"], dtype=float)
    mesh, margin = _mesh_and_margin(entry)
    out_h, out_w = entry["out_shape"]

    src, dtype, close = open_lazy(a.moving)  # lazy (C, H, W) — nothing loaded yet
    try:
        c_n = src.shape[0]
        # bigtiff is mandatory, not an optimisation: a registered slide is written
        # uncompressed at full resolution, so classic TIFF's 32-bit offsets overflow
        # (struct.error: 'I' format requires 0 <= number <= 4294967295) the moment the
        # output crosses 4 GB -- C x H x W x itemsize, reached by any real WSI.
        with tifffile.TiffWriter(str(a.out), bigtiff=True) as tw:
            tw.write(
                stream_tiles(src, m0, mesh, margin, out_h, out_w, a.out_tile, dtype),
                shape=(c_n, out_h, out_w),
                dtype=dtype,
                tile=(a.out_tile, a.out_tile),
                photometric="minisblack",
                # The stitched slide used to be written with no scale of any kind, so
                # everything downstream of the STARE path -- SPLIT_CHANNELS, and through
                # it the published pyramid -- had nothing but params.pixel_size to go on
                # and no way to notice a disagreement. Standard resolution tags rather
                # than an OME header: an OME header here would become a second,
                # channel-less source of channel names for SPLIT_CHANNELS to find.
                # CENTIMETER means pixels-per-cm, i.e. 1e4 µm/cm over µm/px.
                resolution=(1e4 / a.pixel_size, 1e4 / a.pixel_size),
                resolutionunit="CENTIMETER",
            )
    finally:
        close()

    logger.info(
        f"streamed {name}: ({c_n}, {out_h}, {out_w}) -> {a.out} "
        f"(mesh={'yes' if mesh else 'no'}, tile={a.out_tile})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
