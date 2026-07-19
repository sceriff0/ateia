#!/usr/bin/env python3
"""Single-variant periodic illumination correction (pipeline-facing CLI).

Reads a stitched mosaic (.nd2 or .tif/.tiff), applies one correction variant,
and writes a corrected OME-TIFF. Channel names and pixel size are taken from
the input's metadata (ND2 / OME) unless --channels overrides them.
"""
from __future__ import annotations
import argparse, os, sys, pathlib
import tifffile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant
from illum.io import load_mosaic


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True, help="stitched mosaic (.nd2 or .tif/.tiff)")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--channels", nargs="+",
                   help="channel names in order; optional for .nd2 (read from metadata)")
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--flatfield", default="periodic", choices=["none", "periodic", "basic"])
    p.add_argument("--dark", default="none", choices=["none", "const", "field"])
    p.add_argument("--background", default="none")
    p.add_argument("--smooth-frac", type=float, default=0.12)
    args = p.parse_args()
    if args.flatfield == "basic" and args.dark != "none":
        raise SystemExit("error: --flatfield basic performs its own darkfield estimation; "
                         "use --dark none with --flatfield basic")

    os.makedirs(args.output_dir, exist_ok=True)
    stack, names, px = load_mosaic(args.image, args.channels)
    print(f"loaded {args.image}: shape {stack.shape}, channels {names}, pixel {px} um")

    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: pitch_x={grid['pitch_x']:.2f} pitch_y={grid['pitch_y']:.2f} "
          f"phase_x={grid['phase_x']} phase_y={grid['phase_y']}")

    v = Variant(name="cli", flatfield=args.flatfield, dark=args.dark,
                background=args.background, smooth_frac=args.smooth_frac)
    res = run_variant(stack, grid, v, out_dtype=stack.dtype)

    stem = pathlib.Path(args.image).name
    for suf in (".ome.tif", ".ome.tiff", ".nd2", ".tif", ".tiff"):
        if stem.lower().endswith(suf):
            stem = stem[:-len(suf)]
            break
    out_path = os.path.join(args.output_dir, f"{stem}_periodic.ome.tif")
    tifffile.imwrite(out_path, res["corrected"], photometric="minisblack",
                     bigtiff=True, ome=True, metadata={
                         "axes": "CYX", "Channel": {"Name": names[:stack.shape[0]]},
                         "PhysicalSizeX": px, "PhysicalSizeXUnit": "µm",
                         "PhysicalSizeY": px, "PhysicalSizeYUnit": "µm"})
    print(f"wrote {out_path}  metrics={res['metrics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
