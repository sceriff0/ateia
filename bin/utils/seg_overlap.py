"""Segmentation-overlap metrics for in-pipeline registration QC (reg_qc=2).

Given two label masks segmented independently on co-registered slides (reference vs a
moving round, both already on the aligned grid), score how well the segmented nuclei
overlap: foreground **Dice** / **IoU** (areal agreement) and **instance-F1** (per-cell
agreement via greedy IoU matching). Dice responds to sub-cell shifts; instance-F1 has a
resolution floor at the IoU threshold — reporting both captures different error scales.

Self-contained (numpy only) so the pipeline does not depend on the benchmarks tree.

**Memory contract.** Every statistic here is accumulated over row blocks, so peak RAM is
O(block) + O(labels) + O(distinct overlapping label pairs) — never O(slide area) beyond
the two label images the caller already holds. The previous implementation raveled both
masks to ``int64`` up front, which tripled the footprint of a whole-slide raster and was
the dominant term in the WARP_SEG_QC exit-140 kills.
"""
from __future__ import annotations

import numpy as np

# One row block of both label images plus the transient intp copies bincount makes
# stays in the tens of MB at any slide width. Purely an internal knob: every metric
# is an exact accumulation, so block size changes performance, never results.
_BLOCK_BYTES = 64 << 20
_BYTES_PER_PX = 24  # 2 label px + intp copies + bool mask, rounded up


def _row_blocks(h: int, w: int):
    """Yield row slices covering ``h`` rows, sized to the block budget."""
    rows = max(1, int(_BLOCK_BYTES // max(1, w * _BYTES_PER_PX)))
    for r0 in range(0, h, rows):
        yield slice(r0, min(r0 + rows, h))


def _bincountable(block):
    """np.bincount casts to intp internally; uint64 has no safe cast, so narrow it."""
    if block.dtype == np.uint64:
        return block.astype(np.int64, copy=False)
    return block


def align_shapes(a, b):
    """Crop both masks to their common top-left extent.

    Registered and reference slides share the aligned grid, but a stray row/col
    difference must not crash the metric — crop to the smaller extent."""
    a = np.asarray(a); b = np.asarray(b)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    return a[:h, :w], b[:h, :w]


class _Overlap:
    """Block-wise accumulation of everything the three metrics need, in one pass.

    ``area_a`` / ``area_b`` are per-label pixel counts (index 0 = background).
    ``pair_idx`` / ``pair_counts`` are the *sparse* co-occurrence histogram of
    encoded ``a * stride + b`` values, restricted to pixels foreground in both.
    """

    __slots__ = ("area_a", "area_b", "pair_idx", "pair_counts", "inter_px", "stride")

    def __init__(self, area_a, area_b, pair_idx, pair_counts, inter_px, stride):
        self.area_a = area_a
        self.area_b = area_b
        self.pair_idx = pair_idx
        self.pair_counts = pair_counts
        self.inter_px = inter_px
        self.stride = stride

    @property
    def fg_a(self) -> int:
        return int(self.area_a[1:].sum())

    @property
    def fg_b(self) -> int:
        return int(self.area_b[1:].sum())

    @property
    def n_a(self) -> int:
        """Labels *actually present* in the raster — not ``max()``.

        A label can be assigned by the rasterizer and then be fully overwritten by a
        later overlapping cell, or come from a degenerate ring that covers no pixel.
        Counting ``max()`` inflated the recall/precision denominators by exactly those
        cells, which silently depressed instance-F1 on dense tissue."""
        return int(np.count_nonzero(self.area_a[1:]))

    @property
    def n_b(self) -> int:
        return int(np.count_nonzero(self.area_b[1:]))


def _accumulate(mask_a, mask_b) -> _Overlap:
    a, b = align_shapes(mask_a, mask_b)
    h, w = int(a.shape[0]), int(a.shape[1])
    na_max = int(a.max()) if a.size else 0
    nb_max = int(b.max()) if b.size else 0
    stride = nb_max + 1
    area_a = np.zeros(na_max + 1, dtype=np.int64)
    area_b = np.zeros(nb_max + 1, dtype=np.int64)
    idx_parts: list = []
    cnt_parts: list = []
    inter_px = 0

    for sl in _row_blocks(h, w):
        ablk = _bincountable(np.ascontiguousarray(a[sl]).ravel())
        bblk = _bincountable(np.ascontiguousarray(b[sl]).ravel())
        area_a += np.bincount(ablk, minlength=na_max + 1)
        area_b += np.bincount(bblk, minlength=nb_max + 1)
        fg = (ablk > 0) & (bblk > 0)
        k = int(np.count_nonzero(fg))
        if k == 0:
            continue
        inter_px += k
        # Promote only the overlapping foreground pixels to int64 for the encoding.
        # Widening the whole block (let alone the whole mask) is what blew the budget.
        pair = ablk[fg].astype(np.int64) * stride + bblk[fg].astype(np.int64)
        i, c = np.unique(pair, return_counts=True)
        idx_parts.append(i)
        cnt_parts.append(c.astype(np.int64))

    if idx_parts:
        all_idx = np.concatenate(idx_parts)
        all_cnt = np.concatenate(cnt_parts)
        # Merge the per-block sparse histograms: sized by distinct pairs, not pixels.
        pair_idx, inv = np.unique(all_idx, return_inverse=True)
        pair_counts = np.bincount(inv, weights=all_cnt, minlength=pair_idx.size).astype(np.int64)
    else:
        pair_idx = np.empty(0, dtype=np.int64)
        pair_counts = np.empty(0, dtype=np.int64)

    return _Overlap(area_a, area_b, pair_idx, pair_counts, inter_px, stride)


def dice(mask_a, mask_b):
    ov = _accumulate(mask_a, mask_b)
    denom = ov.fg_a + ov.fg_b
    if denom == 0:
        return float("nan")
    return float(2 * ov.inter_px / denom)


def iou(mask_a, mask_b):
    ov = _accumulate(mask_a, mask_b)
    union = ov.fg_a + ov.fg_b - ov.inter_px
    if union == 0:
        return float("nan")
    return float(ov.inter_px / union)


def _instance_f1_from(ov: _Overlap, iou_thresh: float) -> dict:
    na, nb = ov.n_a, ov.n_b
    nan = float("nan")
    if na == 0 or nb == 0:
        return {"f1": nan, "precision": nan, "recall": nan, "matched": 0, "n_a": na, "n_b": nb}

    inter = ov.pair_counts.astype(np.float64)
    al = ov.pair_idx // ov.stride
    bl = ov.pair_idx % ov.stride
    area_a = ov.area_a.astype(np.float64)
    area_b = ov.area_b.astype(np.float64)
    iou_vals = inter / (area_a[al] + area_b[bl] - inter)

    # Drop sub-threshold candidates BEFORE sorting: the greedy loop below is Python,
    # and on whole-slide masks the candidate set is millions of pairs of which only
    # the matches can ever be consumed. Exact — sub-threshold pairs break the loop
    # anyway — but turns an O(pairs) Python walk into an O(matches) one.
    keep = iou_vals >= iou_thresh
    al, bl, iou_vals = al[keep], bl[keep], iou_vals[keep]

    used_a, used_b, matched = set(), set(), 0
    for k in np.argsort(-iou_vals):
        a_lab, b_lab = int(al[k]), int(bl[k])
        if a_lab in used_a or b_lab in used_b:
            continue
        used_a.add(a_lab); used_b.add(b_lab); matched += 1

    precision = matched / nb
    recall = matched / na
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"f1": f1, "precision": precision, "recall": recall,
            "matched": matched, "n_a": na, "n_b": nb}


def instance_f1(ma, mb, iou_thresh: float = 0.5) -> dict:
    """Per-cell agreement by greedy IoU matching (the standard detection metric).

    Returns {f1, precision, recall, matched, n_a, n_b}. Intersections come from a flat
    (a,b) label histogram, not an N×N matrix, so it scales to whole-slide masks.
    """
    return _instance_f1_from(_accumulate(ma, mb), iou_thresh)


def evaluate_masks(ref_labels, moving_labels, iou_thresh: float = 0.5) -> dict:
    """Align shapes, then return Dice + IoU + instance-F1 between two label masks.

    Single accumulation pass shared by all three metrics — the masks are read once.
    """
    ov = _accumulate(ref_labels, moving_labels)
    inst = _instance_f1_from(ov, iou_thresh)
    denom = ov.fg_a + ov.fg_b
    union = ov.fg_a + ov.fg_b - ov.inter_px
    return {
        "dice": float(2 * ov.inter_px / denom) if denom else float("nan"),
        "iou": float(ov.inter_px / union) if union else float("nan"),
        "instance_f1": inst["f1"],
        "instance_precision": inst["precision"],
        "instance_recall": inst["recall"],
        "matched": inst["matched"],
        "n_ref": inst["n_a"],
        "n_moving": inst["n_b"],
        "fg_px_ref": ov.fg_a,
        "fg_px_moving": ov.fg_b,
        "fg_px_intersection": ov.inter_px,
    }
