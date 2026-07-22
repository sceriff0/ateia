#!/usr/bin/env python3
"""Benchmark illumination-correction variants on one mosaic.

Default (single process): run every variant sequentially and write per-step
plots, metrics.json, and an HTML report -- plus a QuPath pyramid per variant
unless --no-pyramids.

Parallel (SLURM job array) workflow -- one variant per task, then aggregate:
  --list-variants   print the selected matrix's variant names (one per line)
                    and exit (no --image needed). Used to size the array.
  --variant NAME    run exactly ONE variant; write its pyramid + plots and a
                    self-contained part file at <outdir>/parts/<NAME>.json.
  --aggregate       combine <outdir>/parts/*.json into metrics.json + report.html
                    (no --image needed). Run after the array finishes.

The three modes write into the SAME <outdir>, so an array of `--variant` tasks
followed by one `--aggregate` task produces byte-identical outputs to a single
sequential run.
"""
from __future__ import annotations
import argparse, json, os, sys, pathlib
import numpy as np

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
from illum.grid import recover_grid
from illum.pipeline import DEFAULT_MATRIX, full_matrix, run_variant
from illum.background import available_methods
from illum.plots import plot_grid_recovery, plot_flatfield, plot_before_after
from illum.report import write_report
from illum.io import load_mosaic


def _select_matrix(full):
    return full_matrix(available_methods()) if full else DEFAULT_MATRIX


def _load_stack(args):
    """Load the mosaic (.nd2 or .tif) as (stack, channel_names, pixel_um).

    Channel names and pixel size come from the file's metadata (ND2) or OME
    tags, overridable by --channels. --max-channels truncates both.
    """
    stack, names, pixel_um = load_mosaic(args.image, args.channels)
    if args.max_channels:
        stack = stack[:args.max_channels]
        names = names[:args.max_channels]
    return stack, names, pixel_um


def _grid_png(stack, grid, outdir, approx_tile):
    """Write the shared grid-recovery plot once. Atomic + idempotent so
    concurrent array tasks racing on the same path can't corrupt it."""
    path = outdir / "plots" / "grid.png"
    if not path.exists():
        tmp = outdir / "plots" / f"grid.{os.getpid()}.tmp.png"
        plot_grid_recovery(stack, grid, tmp, approx_tile=approx_tile)
        os.replace(tmp, path)          # atomic: last writer wins, never partial
    return str(path)


def _qc_channel(stack):
    """Index of the DENSEST channel — the one whose eyeball QC shows seams best.

    Channel 0 is often a sparse/dim marker; on a near-empty channel the mean
    profiles can't distinguish 'kept signal' from 'zeroed everything'. The
    densest channel (highest median on a thumbnail — typically DAPI) fills every
    tile, so seams and signal loss are actually visible. Cheap: strided thumbnail.
    """
    if stack.ndim != 3 or stack.shape[0] == 1:
        return 0
    ds = max(max(stack.shape[1:]) // 512, 1)
    med = [float(np.median(stack[c, ::ds, ::ds])) for c in range(stack.shape[0])]
    return int(np.argmax(med))


def _flatfield_png(ff, outdir, channel_name):
    """Write the flat-field plot to a shared per-channel path, once.

    The periodic flat-field depends only on the channel (not on the swept
    darkfield/background axes), so every periodic variant produces the SAME field
    for a given channel — writing one copy per variant is pure redundancy. Shared,
    atomic, idempotent (mirrors _grid_png) so racing array tasks are safe."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(channel_name))
    path = outdir / "plots" / f"flatfield_{safe}.png"
    if not path.exists():
        tmp = outdir / "plots" / f"flatfield_{safe}.{os.getpid()}.tmp.png"
        plot_flatfield(ff, tmp, channel_name)
        os.replace(tmp, path)
    return str(path)


def _run_one(v, stack, grid, outdir, names, pixel_um, no_pyramids, grid_png, qc_idx=0):
    """Compute one variant and write its plots + pyramid.

    Returns (entry, plot_paths). `entry` (name + metrics) is built BEFORE the
    plot/pyramid steps, and those steps are individually guarded, so a plot or
    pyramid failure never loses the variant's recorded metrics. Diagnostic plots
    use the densest channel (`qc_idx`) so the eyeball QC can actually see seams
    and signal loss.
    """
    res = run_variant(stack, grid, v, out_dtype=stack.dtype)
    entry = {"name": res["name"], "metrics": res["metrics"]}
    pngs = [grid_png]
    try:
        if res["flats"][qc_idx] is not None:
            pngs.append(_flatfield_png(res["flats"][qc_idx], outdir, names[qc_idx]))
        pngs.append(plot_before_after(stack[qc_idx], res["corrected"][qc_idx], grid,
                                      outdir / "plots" / f"{v.name}_ba.png", names[qc_idx]))
    except Exception as e:
        print(f"    WARN plot {v.name}: {e}")
    if not no_pyramids:
        try:
            from merge_channels_pyramid import write_pyramidal_ome_tiff, generate_channel_color
            ch_names = names[:stack.shape[0]]
            colors = [generate_channel_color(n, i) for i, n in enumerate(ch_names)]
            write_pyramidal_ome_tiff(res["corrected"],
                                     str(outdir / "pyramids" / f"{v.name}.ome.tiff"),
                                     ch_names, colors,
                                     physical_size_x=pixel_um, physical_size_y=pixel_um)
        except Exception as e:
            print(f"    WARN pyramid {v.name}: {e}")
    return entry, pngs


def _aggregate(outdir):
    parts = sorted((outdir / "parts").glob("*.json"))
    if not parts:
        print(f"no parts found in {outdir/'parts'}; nothing to aggregate")
        return 1
    results, plot_index = [], {}
    for pf in parts:
        d = json.loads(pf.read_text())
        results.append({"name": d["name"], "metrics": d["metrics"]})
        plot_index[d["name"]] = d.get("plots", [])
    (outdir / "metrics.json").write_text(json.dumps(results, indent=2))
    write_report(results, plot_index, outdir / "report.html")
    print(f"aggregated {len(results)} variants -> {outdir/'report.html'} and {outdir/'metrics.json'}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", help="stitched mosaic (.nd2 or .tif/.tiff)")
    p.add_argument("--outdir", required=True)
    p.add_argument("--channels", nargs="+",
                   help="channel names in order; optional for .nd2 (read from "
                        "metadata) — required only for TIFF without OME channel names")
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--full-grid", action="store_true")
    p.add_argument("--no-pyramids", action="store_true")
    p.add_argument("--max-channels", type=int, default=None)
    p.add_argument("--list-variants", action="store_true",
                   help="print selected-matrix variant names (one per line) and exit")
    p.add_argument("--variant", default=None,
                   help="run ONE variant by name (SLURM array task); writes parts/<name>.json")
    p.add_argument("--aggregate", action="store_true",
                   help="combine parts/*.json into metrics.json + report.html")
    args = p.parse_args()

    outdir = pathlib.Path(args.outdir)

    # --- list mode (no image needed) ---
    if args.list_variants:
        for v in _select_matrix(args.full_grid):
            print(v.name)
        return 0

    # --- aggregate mode (no image needed) ---
    if args.aggregate:
        return _aggregate(outdir)

    # --- compute modes need an image (channels auto-derived for ND2/OME) ---
    if not args.image:
        p.error("--image is required unless --list-variants or --aggregate")

    (outdir / "plots").mkdir(parents=True, exist_ok=True)
    if not args.no_pyramids:
        (outdir / "pyramids").mkdir(parents=True, exist_ok=True)

    stack, names, pixel_um = _load_stack(args)
    print(f"loaded {args.image}: shape {stack.shape}, channels {names}, pixel {pixel_um} um")
    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: {grid}")
    grid_png = _grid_png(stack, grid, outdir, args.approx_tile)
    qc_idx = _qc_channel(stack)
    print(f"qc channel for plots: {qc_idx} ({names[qc_idx]})")

    # --- single-variant mode (one SLURM array task) ---
    if args.variant:
        (outdir / "parts").mkdir(parents=True, exist_ok=True)
        match = next((v for v in _select_matrix(True) if v.name == args.variant), None) \
            or next((v for v in DEFAULT_MATRIX if v.name == args.variant), None)
        if match is None:
            p.error(f"unknown variant '{args.variant}'; "
                    f"run with --list-variants --full-grid to see names")
        print(f"--- variant: {match.name}")
        try:
            entry, pngs = _run_one(match, stack, grid, outdir, names, pixel_um,
                                   args.no_pyramids, grid_png, qc_idx)
        except Exception as e:
            # run_variant itself failed -> no part written; aggregate simply
            # omits this variant (one bad variant doesn't sink the grid).
            print(f"    SKIP {match.name}: {e}")
            return 0
        entry["plots"] = pngs
        (outdir / "parts" / f"{match.name}.json").write_text(json.dumps(entry, indent=2))
        print(f"wrote parts/{match.name}.json")
        return 0

    # --- default: sequential over all variants (single process) ---
    results, plot_index = [], {}
    for v in _select_matrix(args.full_grid):
        print(f"--- variant: {v.name}")
        try:
            entry, pngs = _run_one(v, stack, grid, outdir, names, pixel_um,
                                   args.no_pyramids, grid_png, qc_idx)
        except Exception as e:
            print(f"    SKIP {v.name}: {e}")
            continue
        results.append(entry)
        plot_index[v.name] = pngs
    (outdir / "metrics.json").write_text(json.dumps(results, indent=2))
    write_report(results, plot_index, outdir / "report.html")
    print(f"wrote {outdir/'report.html'} and {outdir/'metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
