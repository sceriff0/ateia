"""Benchmark-image registration comparison: classic vs distributed REGISTERED OUTPUTS, pixel for pixel.

The sweep registers the SAME benchmark image classic vs distributed. Two distributed paths, two verdicts:
  - SEPARATED (reg_dist_force_tiling=false): bit-identical to classic by construction -> a PARITY GATE
    (must be pixel-identical; the exit code reflects this).
  - TILED (reg_dist_force_tiling=true): a DIFFERENT algorithm (tiled != whole-image optical flow), so
    NOT expected to match -> a DRIFT MEASUREMENT: how far it moves from classic (max|Δ|, mean|Δ|,
    %pixels differing), by tile_wh / tile_buffer. Informational, does NOT fail the gate.
For the tiled path's registration ACCURACY (not just pixel drift), see the feature-error signal harvested
into quality.csv (enable_feature_error) — plotted by path in plots.R.

Registered slides for a run live at <results_root>/<run_id>/out/<patient>/registered/registered_slides/
*_registered.ome.tiff (see benchmarks/run_sweep.sh + registration.nf publishDir).

CLI:
  python -m benchmarks.registration_eval.compare_registered \
      --results-root RESULTS --run-plan run_plan.csv --drift-csv benchmarks/analysis/registration_drift.csv
Exit code reflects the SEPARATED bit-identical gate only; tiled drift is reported + written to --drift-csv.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np


def find_registered(run_out_dir) -> dict:
    """Map (patient, slide_stem) -> registered ome.tiff path under a run's --outdir tree."""
    root = Path(run_out_dir)
    out = {}
    for f in root.glob("*/registered/registered_slides/*_registered.ome.tiff"):
        patient = f.parents[2].name              # <patient>/registered/registered_slides/<file>
        stem = f.name.replace("_registered.ome.tiff", "")
        out[(patient, stem)] = f
    return out


def _imread(path):
    import tifffile
    return np.asarray(tifffile.imread(str(path)))


def compare_registered_dirs(classic_out, distributed_out, atol: float = 0.0,
                            reader=_imread) -> list[dict]:
    """Compare every slide present in BOTH run dirs. Returns one dict per shared slide with the DRIFT
    of distributed from classic: {patient, slide, equal, max_abs_delta, mean_abs_delta, pct_pixels_diff,
    shape_*, within_atol}. For the separated path these should be ~0 (bit-identical); for the tiled path
    they QUANTIFY the drift (tiled != classic whole-image)."""
    a = find_registered(classic_out)
    b = find_registered(distributed_out)
    shared = sorted(set(a) & set(b))
    results = []
    for key in shared:
        ca, cb = reader(a[key]), reader(b[key])
        if ca.shape != cb.shape:
            results.append({"patient": key[0], "slide": key[1], "equal": False,
                            "max_abs_delta": float("inf"), "mean_abs_delta": float("inf"),
                            "pct_pixels_diff": float("nan"),
                            "shape_classic": ca.shape, "shape_distributed": cb.shape,
                            "within_atol": False})
            continue
        if ca.size:
            delta = np.abs(ca.astype(np.float64) - cb.astype(np.float64))
            mx, mean = float(delta.max()), float(delta.mean())
            pct = float(100.0 * np.count_nonzero(delta) / delta.size)
        else:
            mx = mean = pct = 0.0
        results.append({"patient": key[0], "slide": key[1], "equal": bool(np.array_equal(ca, cb)),
                        "max_abs_delta": mx, "mean_abs_delta": mean, "pct_pixels_diff": pct,
                        "shape_classic": ca.shape, "shape_distributed": cb.shape,
                        "within_atol": mx <= atol})
    return results


def _truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes")


def _auto_pair(results_root, run_plan_csv):
    """Pair each distributed run with its same-cell classic run, LABELLED by path:
      - 'separated' (reg_dist_force_tiling=false): bit-identical parity gate.
      - 'tiled'     (reg_dist_force_tiling=true) : NOT bit-identical — a DRIFT measurement (how far the
        tiled fan-out moves from classic whole-image). Carries tile_wh / tile_buffer for the report.
    """
    import pandas as pd
    plan = pd.read_csv(run_plan_csv)
    is_dist = plan.get("reg_distributed_tiling").map(_truthy)
    is_tiled = (plan["reg_dist_force_tiling"].map(_truthy) if "reg_dist_force_tiling" in plan.columns
                else pd.Series(False, index=plan.index))
    classic = plan[~is_dist]
    if classic.empty or not is_dist.any():
        raise SystemExit("run plan has no classic AND distributed runs to pair")
    root = Path(results_root)
    cell_keys = [k for k in ("target_px", "n_channels", "n_register_images") if k in plan.columns]
    classic_by_cell = {tuple(r[k] for k in cell_keys): r["run_id"] for _, r in classic.iterrows()}
    pairs = []
    for _, dr in plan[is_dist].iterrows():
        key = tuple(dr[k] for k in cell_keys)
        if key not in classic_by_cell:
            continue
        pairs.append({"cell": key, "path": "tiled" if _truthy(dr.get("reg_dist_force_tiling")) else "separated",
                      "tile_wh": dr.get("reg_dist_tile_wh"), "tile_buffer": dr.get("reg_dist_tile_buffer"),
                      "classic_out": root / classic_by_cell[key] / "out",
                      "distributed_out": root / dr["run_id"] / "out"})
    if not pairs:
        raise SystemExit("no classic/distributed run pairs share a cell (size, channels, N)")
    return pairs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classic-out")
    ap.add_argument("--distributed-out")
    ap.add_argument("--results-root")
    ap.add_argument("--run-plan")
    ap.add_argument("--atol", type=float, default=0.0, help="0 == exact (bit-identical, the SEPARATED claim)")
    ap.add_argument("--drift-csv", default=None, help="write per-slide drift rows here (for plotting)")
    a = ap.parse_args(argv)

    if a.results_root and a.run_plan:
        pairs = _auto_pair(a.results_root, a.run_plan)
    elif a.classic_out and a.distributed_out:
        pairs = [{"cell": None, "path": "separated", "tile_wh": None, "tile_buffer": None,
                  "classic_out": a.classic_out, "distributed_out": a.distributed_out}]
    else:
        ap.error("give --classic-out + --distributed-out, or --results-root + --run-plan")

    print("=" * 78)
    print("REGISTRATION vs CLASSIC — SEPARATED = bit-identical gate, TILED = drift measurement")
    print("=" * 78)
    parity_ok, any_sep, any_tiled, drift_rows = True, False, False, []
    for p in pairs:
        results = compare_registered_dirs(p["classic_out"], p["distributed_out"], atol=a.atol)
        cell = "" if p["cell"] is None else f"cell{p['cell']} "
        for r in results:
            r2 = {"path": p["path"], "cell": str(p["cell"]), "tile_wh": p["tile_wh"],
                  "tile_buffer": p["tile_buffer"], **{k: r[k] for k in
                  ("patient", "slide", "max_abs_delta", "mean_abs_delta", "pct_pixels_diff", "equal")}}
            drift_rows.append(r2)
            if p["path"] == "separated":
                any_sep = True
                status = "OK" if r["within_atol"] else "PARITY-FAIL"
                parity_ok = parity_ok and r["within_atol"]
                print(f"  [separated] {cell}{r['slide']:22s} max|Δ|={r['max_abs_delta']:.4g}  [{status}]")
            else:
                any_tiled = True
                tag = f"tile_wh={p['tile_wh']},buf={p['tile_buffer']}"
                print(f"  [tiled]     {cell}{r['slide']:22s} max|Δ|={r['max_abs_delta']:.4g} "
                      f"mean|Δ|={r['mean_abs_delta']:.4g} diff={r['pct_pixels_diff']:.2f}%  ({tag})")
    print("=" * 78)
    if a.drift_csv and drift_rows:
        import pandas as pd
        pd.DataFrame(drift_rows).to_csv(a.drift_csv, index=False)
        print(f"drift rows -> {a.drift_csv}")
    if any_sep:
        print(f"SEPARATED PARITY (must be bit-identical): {'PASS' if parity_ok else 'FAIL'}", flush=True)
    if any_tiled:
        import statistics
        mx = [r["max_abs_delta"] for r in drift_rows if r["path"] == "tiled" and np.isfinite(r["max_abs_delta"])]
        if mx:
            print(f"TILED DRIFT from classic: max|Δ| up to {max(mx):.4g}, median-of-slides "
                  f"{statistics.median(mx):.4g} (informational — tiled is a different algorithm)", flush=True)
    if not (any_sep or any_tiled):
        print("[compare] NO shared registered slides found", flush=True)
        return 2
    return 0 if parity_ok else 1   # exit code reflects the SEPARATED bit-identical gate only


if __name__ == "__main__":
    sys.exit(main())
