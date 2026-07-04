"""Harvest QUALITY + COST signals the pipeline already writes but the resource trace ignores.

The sweep's trace.txt gives RAM/time; this module joins in the *result-quality* the pipeline emits:
  - registration accuracy: feature-based error JSON
      <root>/<run_id>/out/<patient>/feature_distances/*_feature_distances.json  (enable_feature_error)
  - segmentation output size: cell count = max label of <...>/segment*/*cell_mask*.tif
  - segmentation cross-method agreement: pairwise mask IoU + cell-count ratio between methods on the
    SAME image (segmentation_grid runs share the baseline cell).
Plus per-run COST derived from the trace (cpu-hours, gpu-hours, wall-clock, bottleneck stage).

Everything is BEST-EFFORT and robust to missing/failed runs (a CellSAM run that OOMs just contributes
no rows) — so the analysis + plots keep working when some runs fail.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# Processes that run on the GPU (approximate — for a GPU-hours estimate).
GPU_LEAVES = {"SEGMENT", "QUANTIFY", "EXTRACT_CELL_PROPERTIES", "GENERATE_POSTPROCESSING_QC"}


def _leaf(process: str) -> str:
    return str(process).split(":")[-1]


# ─────────────────────────────────────────────────────────── registration accuracy ──
def harvest_registration_error(results_root, run_plan_csv) -> pd.DataFrame:
    """Per-run registration accuracy from the feature-distance JSONs. Aggregates over the moving
    images of a run (median of per-image AFTER-registration median TRE — robust to outlier images).
    Columns: run_id, reg_tre_median_px, reg_tre_mean_px, reg_improvement_pct. Missing runs are skipped."""
    root = Path(results_root)
    plan = pd.read_csv(run_plan_csv)
    rows = []
    for run_id in plan["run_id"]:
        jsons = list((root / str(run_id) / "out").rglob("*_feature_distances.json"))
        med, mean, imp = [], [], []
        for j in jsons:
            try:
                d = json.loads(j.read_text())
            except Exception:
                continue
            fd = (d.get("after_registration") or {}).get("feature_distances") or {}
            if fd.get("median") is not None:
                med.append(fd["median"])
            if fd.get("mean") is not None:
                mean.append(fd["mean"])
            v = (d.get("improvement") or {}).get("distance_reduction_percent")
            if v is not None:
                imp.append(v)
        if not (med or mean):
            continue
        rows.append({
            "run_id": run_id,
            "reg_tre_median_px": float(np.median(med)) if med else float("nan"),
            "reg_tre_mean_px": float(np.mean(mean)) if mean else float("nan"),
            "reg_improvement_pct": float(np.mean(imp)) if imp else float("nan"),
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────── segmentation counts ──
def _cell_masks(run_out_dir):
    # SEGMENT has no explicit publishDir, so masks land under the global-default dir (out/segment/),
    # NOT out/<patient>/segment*/. Search recursively so we find them wherever they are published.
    p = Path(run_out_dir)
    return sorted(set(p.rglob("*_cell_mask.tif")) | set(p.rglob("*_cell_mask.tiff")))


def _n_cells(path, reader):
    # Count DISTINCT non-zero labels (exact even if labels aren't a contiguous 1..N range — max-label
    # would over-count with gaps).
    try:
        u = np.unique(np.asarray(reader(path)))
        return int((u != 0).sum())
    except Exception:
        return None


def harvest_segmentation_counts(results_root, run_plan_csv, reader=None) -> pd.DataFrame:
    """Per-run cell count = max label of the cell mask (I/O-light: one max() per run). Robust to
    missing masks. reader is injectable for tests; defaults to tifffile.imread."""
    if reader is None:
        import tifffile
        reader = tifffile.imread
    root = Path(results_root)
    plan = pd.read_csv(run_plan_csv)
    rows = []
    for run_id in plan["run_id"]:
        masks = _cell_masks(root / str(run_id) / "out")
        counts = [c for c in (_n_cells(m, reader) for m in masks) if c is not None]
        if not counts:
            continue
        rows.append({"run_id": run_id, "n_cells": int(max(counts))})
    return pd.DataFrame(rows)


def instance_f1(ma, mb, iou_thresh=0.5) -> dict:
    """Instance-level agreement between two label masks by GREEDY IoU matching (the standard detection
    metric — far more meaningful than foreground IoU). Returns {f1, precision, recall, matched, n_a, n_b}.
    Efficient: intersections via a flat (a,b) label histogram, not an N×N matrix."""
    a = np.asarray(ma).ravel().astype(np.int64)
    b = np.asarray(mb).ravel().astype(np.int64)
    na, nb = int(a.max()) if a.size else 0, int(b.max()) if b.size else 0
    nan = float("nan")
    if na == 0 or nb == 0:
        return {"f1": nan, "precision": nan, "recall": nan, "matched": 0, "n_a": na, "n_b": nb}
    fg = (a > 0) & (b > 0)
    pair = a[fg] * (nb + 1) + b[fg]                       # unique key per (a_label, b_label)
    counts = np.bincount(pair)
    idx = np.nonzero(counts)[0]
    inter = counts[idx].astype(np.float64)
    al, bl = idx // (nb + 1), idx % (nb + 1)
    area_a = np.bincount(a, minlength=na + 1).astype(np.float64)
    area_b = np.bincount(b, minlength=nb + 1).astype(np.float64)
    iou = inter / (area_a[al] + area_b[bl] - inter)
    used_a, used_b, matched = set(), set(), 0
    for k in np.argsort(-iou):                            # greedy, highest IoU first
        if iou[k] < iou_thresh:
            break
        if al[k] in used_a or bl[k] in used_b:
            continue
        used_a.add(int(al[k])); used_b.add(int(bl[k])); matched += 1
    precision = matched / nb
    recall = matched / na
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"f1": f1, "precision": precision, "recall": recall, "matched": matched, "n_a": na, "n_b": nb}


def segmentation_agreement(results_root, run_plan_csv, reader=None) -> pd.DataFrame:
    """Cross-method agreement on shared cells: for each (target_px, n_channels) segmented by >1 method,
    compare each pair of methods' cell masks — foreground IoU (spatial agreement) + cell-count ratio
    (over/under-segmentation rate). Uses one representative run per (cell, method). Best-effort."""
    if reader is None:
        import tifffile
        reader = tifffile.imread
    root = Path(results_root)
    plan = pd.read_csv(run_plan_csv)
    if "seg_method" not in plan.columns:
        return pd.DataFrame()
    keys = [k for k in ("target_px", "n_channels") if k in plan.columns]
    rows = []
    for cell, g in plan.groupby(keys):
        by_method = {}
        for _, r in g.groupby("seg_method").head(1).iterrows():   # one run per method at this cell
            masks = _cell_masks(root / str(r["run_id"]) / "out")
            if masks:
                by_method[r["seg_method"]] = masks[0]
        methods = sorted(by_method)
        for i in range(len(methods)):
            for k in range(i + 1, len(methods)):
                a, b = methods[i], methods[k]
                try:
                    ma, mb = np.asarray(reader(by_method[a])), np.asarray(reader(by_method[b]))
                except Exception:
                    continue
                if ma.shape != mb.shape:
                    continue
                fa, fb = ma > 0, mb > 0
                inter = int(np.logical_and(fa, fb).sum())
                union = int(np.logical_or(fa, fb).sum())
                na, nb = int(ma.max()), int(mb.max())
                inst = instance_f1(ma, mb)               # IoU-matched per-cell agreement
                row = dict(zip(keys, cell if isinstance(cell, tuple) else (cell,)))
                row.update(method_a=a, method_b=b,
                           foreground_iou=(inter / union if union else float("nan")),
                           instance_f1=inst["f1"], instance_precision=inst["precision"],
                           instance_recall=inst["recall"], matched_cells=inst["matched"],
                           n_cells_a=na, n_cells_b=nb,
                           cell_count_ratio=(na / nb if nb else float("nan")))
                rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────── per-run cost ──
def run_cost_summary(runs_df: pd.DataFrame) -> pd.DataFrame:
    """Per-run cost derived from the trace: total compute, CPU-hours, GPU-hours, end-to-end wall-clock
    (if timestamps present), and the bottleneck stage (largest single-process realtime). One row / run."""
    if runs_df.empty:
        return pd.DataFrame()
    df = runs_df.copy()
    df["_leaf"] = df["process"].map(_leaf)
    key = "run_id" if "run_id" in df.columns else "config_id"
    carry = [c for c in ("varied_axis", "target_px", "n_channels", "n_register_images",
                         "reg_distributed_tiling", "config_id", "rep") if c in df.columns]
    out = []
    for run, g in df.groupby(key):
        rt = g["realtime_s"].fillna(0)
        cpus = g["cpus"].fillna(1) if "cpus" in g else pd.Series(1, index=g.index)
        gpu = g["_leaf"].isin(GPU_LEAVES)
        row = {key: run}
        for c in carry:
            row[c] = g[c].iloc[0]
        row["total_realtime_s"] = float(rt.sum())
        row["cpu_hours"] = float((rt * cpus).sum() / 3600.0)
        row["gpu_hours"] = float(rt[gpu].sum() / 3600.0)
        # end-to-end wall-clock from timestamps if load parsed them (else NaN)
        if {"start_ts", "complete_ts"} <= set(g.columns):
            s, c = g["start_ts"].dropna(), g["complete_ts"].dropna()
            row["wall_clock_s"] = (float((c.max() - s.min()).total_seconds())
                                   if len(s) and len(c) else float("nan"))
        else:
            row["wall_clock_s"] = float("nan")
        if len(rt) and rt.max() > 0:
            row["bottleneck_stage"] = g.loc[rt.idxmax(), "_leaf"]
            row["bottleneck_frac"] = float(rt.max() / rt.sum()) if rt.sum() else float("nan")
        out.append(row)
    return pd.DataFrame(out)
