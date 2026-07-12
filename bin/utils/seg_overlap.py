"""Segmentation-overlap metrics for in-pipeline registration QC (reg_qc=2).

Given two label masks segmented independently on co-registered slides (reference vs a
moving round, both already on the aligned grid), score how well the segmented nuclei
overlap: foreground **Dice** / **IoU** (areal agreement) and **instance-F1** (per-cell
agreement via greedy IoU matching). Dice responds to sub-cell shifts; instance-F1 has a
resolution floor at the IoU threshold — reporting both captures different error scales.

Self-contained (numpy only) so the pipeline does not depend on the benchmarks tree.
"""
from __future__ import annotations

import numpy as np


def _fg(mask):
    return np.asarray(mask) > 0


def align_shapes(a, b):
    """Crop both masks to their common top-left extent.

    Registered and reference slides share the aligned grid, but a stray row/col
    difference must not crash the metric — crop to the smaller extent."""
    a = np.asarray(a); b = np.asarray(b)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    return a[:h, :w], b[:h, :w]


def dice(mask_a, mask_b):
    a, b = _fg(mask_a), _fg(mask_b)
    denom = int(a.sum()) + int(b.sum())
    if denom == 0:
        return float("nan")
    return float(2 * int(np.logical_and(a, b).sum()) / denom)


def iou(mask_a, mask_b):
    a, b = _fg(mask_a), _fg(mask_b)
    union = int(np.logical_or(a, b).sum())
    if union == 0:
        return float("nan")
    return float(int(np.logical_and(a, b).sum()) / union)


def instance_f1(ma, mb, iou_thresh: float = 0.5) -> dict:
    """Per-cell agreement by greedy IoU matching (the standard detection metric).

    Returns {f1, precision, recall, matched, n_a, n_b}. Intersections come from a flat
    (a,b) label histogram, not an N×N matrix, so it scales to whole-slide masks.
    """
    a = np.asarray(ma).ravel().astype(np.int64)
    b = np.asarray(mb).ravel().astype(np.int64)
    na, nb = (int(a.max()) if a.size else 0), (int(b.max()) if b.size else 0)
    nan = float("nan")
    if na == 0 or nb == 0:
        return {"f1": nan, "precision": nan, "recall": nan, "matched": 0, "n_a": na, "n_b": nb}
    fg = (a > 0) & (b > 0)
    pair = a[fg] * (nb + 1) + b[fg]
    counts = np.bincount(pair)
    idx = np.nonzero(counts)[0]
    inter = counts[idx].astype(np.float64)
    al, bl = idx // (nb + 1), idx % (nb + 1)
    area_a = np.bincount(a, minlength=na + 1).astype(np.float64)
    area_b = np.bincount(b, minlength=nb + 1).astype(np.float64)
    iou_vals = inter / (area_a[al] + area_b[bl] - inter)
    used_a, used_b, matched = set(), set(), 0
    for k in np.argsort(-iou_vals):
        if iou_vals[k] < iou_thresh:
            break
        if al[k] in used_a or bl[k] in used_b:
            continue
        used_a.add(int(al[k])); used_b.add(int(bl[k])); matched += 1
    precision = matched / nb
    recall = matched / na
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"f1": f1, "precision": precision, "recall": recall,
            "matched": matched, "n_a": na, "n_b": nb}


def evaluate_masks(ref_labels, moving_labels, iou_thresh: float = 0.5) -> dict:
    """Align shapes, then return Dice + IoU + instance-F1 between two label masks."""
    ref, mov = align_shapes(ref_labels, moving_labels)
    inst = instance_f1(ref, mov, iou_thresh=iou_thresh)
    return {
        "dice": dice(ref, mov),
        "iou": iou(ref, mov),
        "instance_f1": inst["f1"],
        "instance_precision": inst["precision"],
        "instance_recall": inst["recall"],
        "matched": inst["matched"],
        "n_ref": inst["n_a"],
        "n_moving": inst["n_b"],
    }
