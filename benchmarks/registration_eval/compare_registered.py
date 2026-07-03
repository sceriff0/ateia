"""Benchmark-image registration parity: classic vs distributed REGISTERED OUTPUTS, pixel for pixel.

The sweep runs the registration stage twice on the SAME benchmark image (the reg_distributed_tiling
OFAT axis): classic REGISTER and the distributed VALIS_DISTRIBUTED_ADAPTER. The default distributed
path (SEPARATED whole-image non-rigid) is bit-identical to classic by construction, so their published
registered slides must be pixel-identical. This is the end-to-end, real-image counterpart to the
code-level gate in tests/integration/compare_classic_vs_distributed.py — here on the larger benchmark
images the sweep already produces, so no extra pipeline run is needed.

Registered slides for a run live at <results_root>/<run_id>/out/<patient>/registered/registered_slides/
*_registered.ome.tiff (see benchmarks/run_sweep.sh + registration.nf publishDir).

CLI:
  # compare two explicit run output dirs
  python -m benchmarks.registration_eval.compare_registered \
      --classic-out RESULTS/run0000/out --distributed-out RESULTS/run0046/out
  # or auto-pair the classic (reg_distributed_tiling=false) and distributed (=true) runs from a plan
  python -m benchmarks.registration_eval.compare_registered \
      --results-root RESULTS --run-plan run_plan.csv
Exits non-zero if any shared slide differs beyond --atol (default 0 == exact).
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
    """Compare every slide present in BOTH run dirs. Returns one result dict per shared slide:
    {patient, slide, equal, max_abs_delta, shape_classic, shape_distributed, within_atol}."""
    a = find_registered(classic_out)
    b = find_registered(distributed_out)
    shared = sorted(set(a) & set(b))
    results = []
    for key in shared:
        ca, cb = reader(a[key]), reader(b[key])
        if ca.shape != cb.shape:
            results.append({"patient": key[0], "slide": key[1], "equal": False,
                            "max_abs_delta": float("inf"),
                            "shape_classic": ca.shape, "shape_distributed": cb.shape,
                            "within_atol": False})
            continue
        d = float(np.max(np.abs(ca.astype(np.float64) - cb.astype(np.float64)))) if ca.size else 0.0
        results.append({"patient": key[0], "slide": key[1], "equal": bool(np.array_equal(ca, cb)),
                        "max_abs_delta": d, "shape_classic": ca.shape,
                        "shape_distributed": cb.shape, "within_atol": d <= atol})
    return results


def _auto_pair(results_root, run_plan_csv):
    """Return (classic_out, distributed_out) for the baseline classic run and the distributed OFAT run."""
    import pandas as pd
    plan = pd.read_csv(run_plan_csv)

    def _truthy(v):
        return str(v).strip().lower() in ("true", "1", "yes")

    dist = plan[plan.get("reg_distributed_tiling").map(_truthy)]
    classic = plan[~plan.get("reg_distributed_tiling").map(_truthy)]
    if dist.empty or classic.empty:
        raise SystemExit("run plan has no classic/distributed pair (need the reg_distributed_tiling axis)")
    root = Path(results_root)
    return (root / classic.iloc[0]["run_id"] / "out",
            root / dist.iloc[0]["run_id"] / "out")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classic-out")
    ap.add_argument("--distributed-out")
    ap.add_argument("--results-root")
    ap.add_argument("--run-plan")
    ap.add_argument("--atol", type=float, default=0.0, help="0 == exact (bit-identical, the design claim)")
    a = ap.parse_args(argv)

    if a.results_root and a.run_plan:
        classic_out, dist_out = _auto_pair(a.results_root, a.run_plan)
    elif a.classic_out and a.distributed_out:
        classic_out, dist_out = a.classic_out, a.distributed_out
    else:
        ap.error("give --classic-out + --distributed-out, or --results-root + --run-plan")

    results = compare_registered_dirs(classic_out, dist_out, atol=a.atol)
    if not results:
        print(f"[parity] NO shared registered slides between {classic_out} and {dist_out}", flush=True)
        return 2
    print("=" * 78)
    print("BENCHMARK-IMAGE REGISTRATION PARITY — classic vs distributed registered slides")
    print("=" * 78)
    ok = True
    for r in results:
        status = "OK" if r["within_atol"] else "FAIL"
        ok = ok and r["within_atol"]
        print(f"  {r['patient']}/{r['slide']:24s} equal={r['equal']!s:5s} "
              f"max|Δ|={r['max_abs_delta']:.4g}  [{status}]")
    print("=" * 78)
    print(f"PARITY (classic == distributed on benchmark images): {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
