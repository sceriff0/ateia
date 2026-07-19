#!/usr/bin/env python3
"""Single-variant periodic illumination correction (pipeline-facing CLI)."""
from __future__ import annotations
import argparse, os, sys, pathlib
import numpy as np
import tifffile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant


def _read_pixel_size(path, fallback=0.325):
    try:
        with tifffile.TiffFile(path) as tif:
            if tif.ome_metadata:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(tif.ome_metadata)
                px = root.find(".//{*}Pixels")
                if px is not None and px.get("PhysicalSizeX"):
                    return float(px.get("PhysicalSizeX"))
    except Exception:
        pass
    return fallback


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--channels", nargs="+", required=True)
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--flatfield", default="periodic", choices=["none", "periodic", "basic"])
    p.add_argument("--dark", default="none", choices=["none", "const", "field"])
    p.add_argument("--background", default="none")
    p.add_argument("--smooth-frac", type=float, default=0.12)
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    stack = np.squeeze(tifffile.imread(args.image))
    if stack.ndim == 2:
        stack = stack[None]
    px = _read_pixel_size(args.image)

    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: pitch_x={grid['pitch_x']:.2f} pitch_y={grid['pitch_y']:.2f} "
          f"phase_x={grid['phase_x']} phase_y={grid['phase_y']}")

    v = Variant(name="cli", flatfield=args.flatfield, dark=args.dark,
                background=args.background, smooth_frac=args.smooth_frac)
    res = run_variant(stack, grid, v, out_dtype=stack.dtype)

    stem = pathlib.Path(args.image).name
    for suf in (".ome.tif", ".ome.tiff", ".tif", ".tiff"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    out_path = os.path.join(args.output_dir, f"{stem}_periodic.ome.tif")
    tifffile.imwrite(out_path, res["corrected"], photometric="minisblack",
                     bigtiff=True, ome=True, metadata={
                         "axes": "CYX", "Channel": {"Name": args.channels[:stack.shape[0]]},
                         "PhysicalSizeX": px, "PhysicalSizeXUnit": "µm",
                         "PhysicalSizeY": px, "PhysicalSizeYUnit": "µm"})
    print(f"wrote {out_path}  metrics={res['metrics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
