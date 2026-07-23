#!/usr/bin/env python3
"""REG_ASSEMBLE (spec §5.3): join warped output tiles into the final OME-TIFF.

Two modes:
  --write-grid : build the same lazy warp reg_warp_tile.py will build, read the output canvas size
                 off it, and emit grid.json. Nothing is decoded -- the warp is never evaluated.
  (default)    : lazily open every tile_<i>.v, join them row-major, and write the OME-TIFF.

Assembly is streaming: the joined image stays unevaluated until ``tiffsave`` pulls it, and libvips
evaluates the join region-by-region as it writes, so peak RAM is O(one row of tiles), not O(slide).
Tiles are joined edge-to-edge with NO blending, which is correct because each tile is a crop of the
same demand-driven warp (see bin/utils/tile_grid.output_grid). The join is a chain of pairwise
``join`` calls rather than ``arrayjoin`` because the right column and bottom row are short on a
ragged canvas, and ``arrayjoin`` sizes every cell to the maximum and pads the difference.
"""
import argparse
import json
import os
import sys

import pyvips

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

import reg_finalize  # noqa: E402  (also sets the numba/matplotlib cache env before valis loads)
from valis import registration, slide_io  # noqa: E402
from mirage_slide_reader import get_reader_for  # noqa: E402
from tile_grid import output_grid  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True, help="REG_PREP per-slide warp_state.json")
    ap.add_argument("--src-slide", required=True, help="full-res source ome.tiff that was warped")
    ap.add_argument("--field", help="composed field from reg_finalize.py --emit-field-only; "
                                    "omit together with --rigid-only (--write-grid mode only)")
    ap.add_argument("--rigid-only", action="store_true",
                    help="no non-rigid field (dxdy=None); must match reg_warp_tile.py's flag")
    ap.add_argument("--write-grid", help="path to write grid.json, then exit")
    ap.add_argument("--tile-wh", type=int, default=4096, help="output tile size (--write-grid mode)")
    ap.add_argument("--tiles-dir", help="directory of tile_<i>.v from reg_warp_tile.py")
    ap.add_argument("--grid", help="grid.json written by --write-grid")
    ap.add_argument("--out", help="output ome.tiff path")
    ap.add_argument("--compression", default="lzw")
    ap.add_argument("--jvm-heap-gb", type=int, default=None,
                    help="explicit BioFormats JVM heap (GB); only used if the source slide is not "
                         "readable by MirageVipsSlideReader")
    args = ap.parse_args()

    ws = json.load(open(args.warp_state))
    heap_gb = reg_finalize.start_jvm_if_needed(args.src_slide, jvm_heap_gb=args.jvm_heap_gb,
                                               tag="reg_assemble")
    try:
        return _write_grid(args, ws) if args.write_grid else _assemble(args, ws)
    finally:
        if heap_gb:
            registration.kill_jvm()


def _write_grid(args, ws):
    """Emit the output-tile grid. The canvas size comes from the real warp rather than from
    ws["aligned_slide_shape_rc"] directly, so the grid can never disagree with what
    reg_warp_tile.py actually produces (that script re-checks the match before cropping)."""
    if args.rigid_only == bool(args.field):
        raise SystemExit("pass exactly one of --field or --rigid-only")
    dxdy = None if args.rigid_only else pyvips.Image.new_from_file(args.field)
    warped, _ = reg_finalize.warp_source(args.src_slide, ws, dxdy)
    grid = output_grid(warped.width, warped.height, args.tile_wh)
    os.makedirs(os.path.dirname(os.path.abspath(args.write_grid)) or ".", exist_ok=True)
    with open(args.write_grid, "w") as fh:
        json.dump(grid, fh)
    print(f"[reg_assemble] grid {grid['n_cols']}x{grid['n_rows']} "
          f"({len(grid['tiles'])} tiles of {args.tile_wh}px) for "
          f"{warped.width}x{warped.height} -> {args.write_grid}", flush=True)
    return 0


def _assemble(args, ws):
    for flag in ("tiles_dir", "grid", "out"):
        if not getattr(args, flag):
            raise SystemExit(f"--{flag.replace('_', '-')} is required unless --write-grid is given")
    if args.field or args.rigid_only:
        # Assembly consumes only the tiles; it never re-warps. Accepting these silently would let a
        # mis-wired Nextflow channel look like it was honoured.
        raise SystemExit("--field/--rigid-only apply to --write-grid only, not to assembly")
    grid = json.load(open(args.grid))

    rows = []
    for r in range(grid["n_rows"]):
        row_tiles = sorted((t for t in grid["tiles"] if t["y"] == r * grid["tile_wh"]),
                           key=lambda t: t["x"])
        if len(row_tiles) != grid["n_cols"]:
            # Rows are selected by exact y == r*tile_wh. A grid whose tiles do not sit on that
            # lattice yields a short row, and pyvips' join defaults to expand=False -- it would
            # CROP to the smaller input rather than fail, quietly truncating the slide.
            raise SystemExit(f"row {r} of {args.grid} has {len(row_tiles)} tiles, expected "
                             f"{grid['n_cols']} -- grid.json is not a regular {grid['tile_wh']}px "
                             "lattice and assembling it would crop the slide")
        rows.append(_join([_open_tile(args.tiles_dir, t) for t in row_tiles], "horizontal"))
    full = _join(rows, "vertical")
    if (full.width, full.height) != (grid["width"], grid["height"]):
        raise SystemExit(f"assembled {full.width}x{full.height} but grid declares "
                         f"{grid['width']}x{grid['height']} -- tiles do not tile the canvas")

    # Channel names come from the SOURCE slide, matching reg_finalize's own save path: the warp
    # changes geometry, never the channel identity or order.
    reader = get_reader_for(args.src_slide, series=ws.get("series"))(
        args.src_slide, series=ws.get("series"))
    names = list(reader.metadata.channel_names or [])
    if len(names) < full.bands:
        names += [f"C{i}" for i in range(len(names), full.bands)]
    tile_wh = min(reader.metadata.optimal_tile_wh, full.width, full.height)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    reg_finalize._save_ome_pyvips(full, args.out, names[:full.bands],
                                  slide_io.vips2bf_dtype(full.format), tile_wh, args.compression)
    print(f"[reg_assemble] wrote {args.out} ({full.width}x{full.height} bands={full.bands}, "
          f"{len(grid['tiles'])} tiles)", flush=True)
    return 0


def _open_tile(tiles_dir, t):
    """Open one tile lazily and check it is the region the grid says it is. A silently-missing or
    wrong-sized tile (a failed fan-out task) would otherwise be padded by ``join`` and shift every
    tile after it, producing a plausible-looking but misregistered slide."""
    path = os.path.join(tiles_dir, f"tile_{t['idx']}.v")
    if not os.path.exists(path):
        raise SystemExit(f"missing tile {path} -- the fan-out did not produce every tile")
    img = pyvips.Image.new_from_file(path)
    if (img.width, img.height) != (t["w"], t["h"]):
        raise SystemExit(f"{path} is {img.width}x{img.height} but the grid declares "
                         f"{t['w']}x{t['h']} @ {t['x']},{t['y']}")
    return img


def _join(imgs, direction):
    out = imgs[0]
    for i in imgs[1:]:
        out = out.join(i, direction)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
