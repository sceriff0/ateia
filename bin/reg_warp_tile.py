#!/usr/bin/env python3
"""REG_WARP_TILE (spec §5.3): warp ONE output tile of one slide.

Bit-identical to the whole-image warp BY CONSTRUCTION, not by approximation: this runs the same
lazy ``reg_finalize.warp_source()`` and then ``.crop()``s the requested region. pyvips is
demand-driven, so cropping the OUTPUT pulls exactly the source pixels that output region needs --
including the interpolation kernel's halo across the tile edge -- computed by the same VALIS
``warp_tools.warp_img`` code as the whole-image case. Hence no halo maths and no seam blending
anywhere in this path; tiles are joined edge-to-edge by reg_assemble.py.

Peak RAM is therefore O(one output tile + its source footprint), not O(slide), which is the whole
point: N of these run independently (one Nextflow task each) instead of one process holding a
full-resolution slide in memory.
"""
import argparse
import json
import os
import sys

import pyvips

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

import reg_finalize  # noqa: E402  (also sets the numba/matplotlib cache env before valis loads)
from valis import registration  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True, help="REG_PREP per-slide warp_state.json")
    ap.add_argument("--field", help="composed padded field from reg_finalize.py --emit-field-only; "
                                    "omit together with --rigid-only for the reference slide")
    ap.add_argument("--rigid-only", action="store_true",
                    help="no non-rigid field (dxdy=None), matching reg_finalize.py --rigid-only")
    ap.add_argument("--src-slide", required=True, help="full-res source ome.tiff to warp")
    ap.add_argument("--grid", required=True, help="grid.json from reg_assemble.py --write-grid")
    ap.add_argument("--tile-idx", type=int, required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--jvm-heap-gb", type=int, default=None,
                    help="explicit BioFormats JVM heap (GB); only used if the source slide is not "
                         "readable by MirageVipsSlideReader")
    args = ap.parse_args()

    if args.rigid_only == bool(args.field):
        raise SystemExit("pass exactly one of --field or --rigid-only")

    ws = json.load(open(args.warp_state))
    grid = json.load(open(args.grid))
    tile = next((t for t in grid["tiles"] if t["idx"] == args.tile_idx), None)
    if tile is None:
        raise SystemExit(f"--tile-idx {args.tile_idx} is not in {args.grid} "
                         f"(it has {len(grid['tiles'])} tiles, 0..{len(grid['tiles']) - 1})")

    heap_gb = reg_finalize.start_jvm_if_needed(args.src_slide, jvm_heap_gb=args.jvm_heap_gb,
                                               tag="reg_warp_tile")

    # dxdy=None is the rigid-only path, byte-for-byte what reg_finalize.py --rigid-only does.
    # Do NOT substitute a zero field: warp_img branches differently on None vs a supplied field,
    # and equivalence between those two branches has never been established.
    dxdy = None if args.rigid_only else pyvips.Image.new_from_file(args.field)
    warped, _ = reg_finalize.warp_source(args.src_slide, ws, dxdy)

    if (warped.width, warped.height) != (grid["width"], grid["height"]):
        # The grid was computed from this same warp by reg_assemble --write-grid; a mismatch means
        # the grid belongs to a different slide or warp state, and assembling would silently
        # produce a cropped or misaligned slide.
        raise SystemExit(
            f"grid canvas {grid['width']}x{grid['height']} != warped canvas "
            f"{warped.width}x{warped.height} for {args.src_slide} -- wrong grid.json/warp state")

    region = warped.crop(tile["x"], tile["y"], tile["w"], tile["h"])

    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, f"tile_{args.tile_idx}.v")
    region.write_to_file(out)
    print(f"[reg_warp_tile] {out} ({tile['w']}x{tile['h']} @ {tile['x']},{tile['y']} "
          f"of {grid['width']}x{grid['height']}, bands={region.bands})", flush=True)
    if heap_gb:
        registration.kill_jvm()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
