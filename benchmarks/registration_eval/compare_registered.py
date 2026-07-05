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


def _shape(path):
    """Cheap image shape from the TIFF header — no full-res load (so the size guard costs nothing)."""
    import tifffile
    with tifffile.TiffFile(str(path)) as tf:
        return tf.series[0].shape


def _delta_from_arrays(ca, cb):
    """Drift stats from two in-memory arrays (or None for a shape mismatch)."""
    if ca.shape != cb.shape:
        return None
    if ca.size:
        d = np.abs(ca.astype(np.float64) - cb.astype(np.float64))
        return {"max": float(d.max()), "mean": float(d.mean()),
                "pct": float(100.0 * np.count_nonzero(d) / d.size), "equal": bool(np.array_equal(ca, cb)),
                "shape": ca.shape}
    return {"max": 0.0, "mean": 0.0, "pct": 0.0, "equal": True, "shape": ca.shape}


def _streamed_delta(pa, pb, block_rows=2048):
    """WHOLE-IMAGE drift computed in horizontal strips off the tiled OME-TIFFs (zarr-backed) — bounded
    memory, so a 65536x65536 slide compares without loading 34 GB. Returns None if zarr is unavailable
    (caller falls back to a full read). {'shape_mismatch': ...} if the two shapes differ."""
    import tifffile
    try:
        import zarr
    except Exception:
        return None
    za = zarr.open(tifffile.imread(str(pa), aszarr=True), mode="r")
    zb = zarr.open(tifffile.imread(str(pb), aszarr=True), mode="r")
    if tuple(za.shape) != tuple(zb.shape):
        return {"shape_mismatch": (tuple(za.shape), tuple(zb.shape))}
    h = za.shape[-2] if za.ndim >= 2 else za.shape[0]        # strip along the Y axis (works for YX / CYX)
    mx = msum = nz = tot = 0.0
    for r0 in range(0, h, block_rows):
        r1 = min(r0 + block_rows, h)
        a = np.asarray(za[..., r0:r1, :] if za.ndim >= 2 else za[r0:r1]).astype(np.float64)
        b = np.asarray(zb[..., r0:r1, :] if zb.ndim >= 2 else zb[r0:r1]).astype(np.float64)
        d = np.abs(a - b)
        if d.size:
            mx = max(mx, float(d.max())); msum += float(d.sum()); nz += int(np.count_nonzero(d)); tot += d.size
    return {"max": mx, "mean": (msum / tot if tot else 0.0), "pct": (100.0 * nz / tot if tot else 0.0),
            "equal": mx == 0, "shape": tuple(za.shape)}


def _compare_one(pa, pb, base, atol, stream, max_pixels, reader, shape_reader) -> dict:
    """Drift row for ONE slide pair. May raise (a mid-write / corrupt file) — the caller isolates that."""
    s = _streamed_delta(pa, pb) if stream else None
    if s is not None and "shape_mismatch" not in s:
        return {**base, "equal": s["equal"], "max_abs_delta": s["max"],
                "mean_abs_delta": s["mean"], "pct_pixels_diff": s["pct"],
                "shape_classic": s["shape"], "shape_distributed": s["shape"],
                "within_atol": s["max"] <= atol}
    if s is not None and "shape_mismatch" in s:
        sa, sb = s["shape_mismatch"]
        return {**base, "equal": False, "max_abs_delta": float("inf"),
                "mean_abs_delta": float("inf"), "pct_pixels_diff": float("nan"),
                "shape_classic": sa, "shape_distributed": sb, "within_atol": False}
    # full-read path (streaming off, or zarr unavailable) — guard against OOM on huge slides
    if max_pixels is not None:
        try:
            npix = max(int(np.prod(shape_reader(pa))), int(np.prod(shape_reader(pb))))
        except Exception:
            npix = 0
        if npix > max_pixels:
            return {**base, "equal": None, "max_abs_delta": float("nan"),
                    "mean_abs_delta": float("nan"), "pct_pixels_diff": float("nan"),
                    "shape_classic": None, "shape_distributed": None,
                    "within_atol": True, "skipped_too_large": True}
    ca, cb = reader(pa), reader(pb)
    d = _delta_from_arrays(ca, cb)
    if d is None:
        return {**base, "equal": False, "max_abs_delta": float("inf"),
                "mean_abs_delta": float("inf"), "pct_pixels_diff": float("nan"),
                "shape_classic": ca.shape, "shape_distributed": cb.shape, "within_atol": False}
    return {**base, "equal": d["equal"], "max_abs_delta": d["max"], "mean_abs_delta": d["mean"],
            "pct_pixels_diff": d["pct"], "shape_classic": d["shape"],
            "shape_distributed": d["shape"], "within_atol": d["max"] <= atol}


def compare_registered_dirs(classic_out, distributed_out, atol: float = 0.0, reader=_imread,
                            max_pixels=None, shape_reader=_shape, stream=False) -> list[dict]:
    """Compare every slide present in BOTH run dirs, returning the DRIFT of distributed from classic per
    slide: {patient, slide, equal, max_abs_delta, mean_abs_delta, pct_pixels_diff, shape_*, within_atol}.
    Separated path ~0 (bit-identical); tiled path QUANTIFIES the drift.

    stream=True compares the WHOLE image in memory-safe strips (zarr) — use on the cluster so even the
    65536px slides get real whole-image parity; falls back to a full read if zarr is missing.
    max_pixels (fallback only): if streaming is off/unavailable, skip a slide larger than this rather
    than OOM (records skipped_too_large; not a parity failure — separated bit-identity holds above).

    Safe against a PARTIALLY-COMPLETE sweep: a slide that can't be read yet (mid-publish, or a corrupt
    in-flight file) is recorded as ``pending=True`` (equal=None, within_atol=True — NOT a parity failure)
    instead of aborting the comparison, so already-finished pairs still report."""
    a = find_registered(classic_out)
    b = find_registered(distributed_out)
    shared = sorted(set(a) & set(b))
    results = []
    for key in shared:
        base = {"patient": key[0], "slide": key[1]}
        try:
            results.append(_compare_one(a[key], b[key], base, atol, stream, max_pixels, reader, shape_reader))
        except Exception as e:
            # The run's slide exists in the tree but isn't readable yet (still being written by a live
            # sweep) or is corrupt. Treat as PENDING, not a parity failure — don't abort the whole run.
            results.append({**base, "equal": None, "max_abs_delta": float("nan"),
                            "mean_abs_delta": float("nan"), "pct_pixels_diff": float("nan"),
                            "shape_classic": None, "shape_distributed": None,
                            "within_atol": True, "pending": True, "error": str(e)[:200]})
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
    ap.add_argument("--no-stream", action="store_true",
                    help="disable whole-image streaming (strip-by-strip via zarr); read full arrays")
    ap.add_argument("--max-dim", type=int, default=0,
                    help="FALLBACK only (when streaming is off/zarr missing): skip slides whose larger "
                         "dim exceeds this to avoid OOM. 0 = no limit (whole-image, needs zarr).")
    a = ap.parse_args(argv)
    max_pixels = a.max_dim ** 2 if a.max_dim and a.max_dim > 0 else None
    stream = not a.no_stream

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
    pairs_total, pairs_with_data, pending_pairs = len(pairs), 0, []
    for p in pairs:
        results = compare_registered_dirs(p["classic_out"], p["distributed_out"], atol=a.atol,
                                          max_pixels=max_pixels, stream=stream)
        cell = "" if p["cell"] is None else f"cell{p['cell']} "
        # A pair "has data" only if at least one slide was actually compared (not absent, pending, or
        # skipped-too-large). This is what makes a mid-sweep run honest: the parity verdict below covers
        # only the pairs that were truly measured, and the coverage line reports the rest.
        if any(not r.get("pending") and not r.get("skipped_too_large") for r in results):
            pairs_with_data += 1
        else:
            pending_pairs.append((p["path"], p["cell"]))
        for r in results:
            if r.get("skipped_too_large"):
                print(f"  [{p['path']:9s}] {cell}{r['slide']:22s} (skipped — larger than --max-dim)")
                continue
            if r.get("pending"):
                print(f"  [{p['path']:9s}] {cell}{r['slide']:22s} (pending — slide not readable yet, "
                      f"sweep still running)")
                continue
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
    # Coverage — so a run against a still-populating results/ dir can't be mistaken for the full picture.
    provisional = ""
    if pending_pairs:
        provisional = f"  (PROVISIONAL — {len(pending_pairs)} pair(s) pending; rerun when the sweep finishes)"
        print(f"coverage: {pairs_with_data}/{pairs_total} classic-distributed pairs had both sides' "
              f"registered slides present; {len(pending_pairs)} pending (still running / not published yet)",
              flush=True)
    else:
        print(f"coverage: {pairs_with_data}/{pairs_total} classic-distributed pairs measured (complete)",
              flush=True)
    if any_sep:
        print(f"SEPARATED PARITY (must be bit-identical): {'PASS' if parity_ok else 'FAIL'}{provisional}",
              flush=True)
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
