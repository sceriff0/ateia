#!/usr/bin/env python3
"""Benchmark illumination-correction variants on one mosaic.

Produces per variant: corrected OME-TIFF, QuPath pyramid, diagnostic plots,
plus a cross-variant metrics.json and an HTML report with a recommendation.
"""
from __future__ import annotations
import argparse, json, sys, pathlib
import numpy as np
import tifffile

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
from illum.grid import recover_grid
from illum.pipeline import DEFAULT_MATRIX, full_matrix, run_variant
from illum.background import available_methods
from illum.plots import plot_grid_recovery, plot_flatfield, plot_before_after
from illum.report import write_report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--channels", nargs="+", required=True)
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--full-grid", action="store_true")
    p.add_argument("--no-pyramids", action="store_true")
    p.add_argument("--max-channels", type=int, default=None)
    args = p.parse_args()

    outdir = pathlib.Path(args.outdir)
    (outdir / "plots").mkdir(parents=True, exist_ok=True)
    if not args.no_pyramids:
        (outdir / "pyramids").mkdir(parents=True, exist_ok=True)

    stack = np.squeeze(tifffile.imread(args.image))
    if stack.ndim == 2:
        stack = stack[None]
    if args.max_channels:
        stack = stack[:args.max_channels]

    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: {grid}")

    variants = full_matrix(available_methods()) if args.full_grid else DEFAULT_MATRIX

    # grid-recovery plot once (geometry shared across channels/variants)
    plot_index = {}
    grid_png = plot_grid_recovery(stack, grid, outdir / "plots" / "grid.png",
                                  approx_tile=args.approx_tile)

    results = []
    for v in variants:
        print(f"--- variant: {v.name}")
        try:
            res = run_variant(stack, grid, v, out_dtype=stack.dtype)
        except Exception as e:
            print(f"    SKIP {v.name}: {e}")
            continue
        results.append({"name": res["name"], "metrics": res["metrics"]})

        try:
            pngs = [grid_png]
            if res["flats"][0] is not None:
                pngs.append(plot_flatfield(res["flats"][0], outdir / "plots" / f"{v.name}_ff.png",
                                           args.channels[0]))
            pngs.append(plot_before_after(stack[0], res["corrected"][0], grid,
                                          outdir / "plots" / f"{v.name}_ba.png", args.channels[0]))
            plot_index[v.name] = pngs
        except Exception as e:
            print(f"    WARN plot {v.name}: {e}")

        if not args.no_pyramids:
            try:
                from merge_channels_pyramid import write_pyramidal_ome_tiff, generate_channel_color
                colors = [generate_channel_color(n, i) for i, n in enumerate(args.channels[:stack.shape[0]])]
                write_pyramidal_ome_tiff(res["corrected"], str(outdir / "pyramids" / f"{v.name}.ome.tiff"),
                                         args.channels[:stack.shape[0]], colors)
            except Exception as e:
                print(f"    WARN pyramid {v.name}: {e}")

    (outdir / "metrics.json").write_text(json.dumps(results, indent=2))
    write_report(results, plot_index, outdir / "report.html")
    print(f"wrote {outdir/'report.html'} and {outdir/'metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
