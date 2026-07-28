#!/usr/bin/env python3
"""STARE fan-out step 4/4: warp the moving slide through the manifest and write the registered slide.

Applies the manifest's M0 + mesh to every channel of the moving slide (bilinear, non-negative) and
writes the registered OME-TIFF in the reference frame. Warps in horizontal strips so peak memory is
a strip, not the whole slide.
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
from logger import configure_logging, get_logger  # noqa: E402
from mesh_field import MeshField  # noqa: E402
from tiled_io import load_channels, write_ome_nonneg  # noqa: E402
from tiled_warp import warp_image  # noqa: E402

logger = get_logger(__name__)


def _entry_for(manifest, moving_name):
    slides = manifest["slides"]
    if moving_name and moving_name in slides:
        return moving_name, slides[moving_name]
    movers = [k for k in slides if k != manifest["ref_slide"]]
    if not movers:
        raise ValueError("manifest carries no moving slide")
    return movers[0], slides[movers[0]]


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(
        description="STARE stitch: warp the moving slide via the manifest."
    )
    ap.add_argument("--moving", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--moving-name", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--strip", type=int, default=2048, help="rows warped per strip (memory bound)"
    )
    a = ap.parse_args(argv)

    manifest = json.loads(Path(a.manifest).read_text())
    name, entry = _entry_for(manifest, a.moving_name)
    m0 = np.asarray(entry["M0"], dtype=float)
    mesh = None
    if entry.get("mesh") is not None:
        m = entry["mesh"]
        mesh = MeshField(
            m["grid_x"], m["grid_y"], np.asarray(m["displacements"], dtype=float)
        )
    out_h, out_w = entry["out_shape"]

    src = load_channels(a.moving)  # (C, H, W)
    mov_hwc = np.moveaxis(src, 0, -1).astype(np.float32)  # (H, W, C)

    # Warp in row strips so peak memory is one strip of output, not the whole slide.
    out = np.empty((out_h, out_w, src.shape[0]), dtype=np.float32)
    for y0 in range(0, out_h, a.strip):
        y1 = min(y0 + a.strip, out_h)
        out[y0:y1] = warp_image(mov_hwc, m0, mesh, (y1 - y0, out_w), out_origin=(0, y0))

    write_ome_nonneg(a.out, np.moveaxis(out, -1, 0), src.dtype)
    logger.info(
        f"stitched {name}: {out.shape} -> {a.out} (mesh={'yes' if mesh else 'no'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
