"""Tests for bin/utils/seg_overlap.py — the self-contained overlap metrics behind
the in-pipeline segmentation registration QC (reg_qc=2).

Pure numpy: no StarDist/TensorFlow/valis needed. The 128x128 label-mask fixtures in
tests/testdata exercise the real instance-matching path.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from utils import seg_overlap


# ── foreground Dice / IoU ──────────────────────────────────────────────────────
def test_dice_identical_and_disjoint():
    a = np.zeros((4, 4), dtype=np.uint32); a[0:2, 0:2] = 1
    assert seg_overlap.dice(a, a.copy()) == 1.0
    c = np.zeros((4, 4), dtype=np.uint32); c[2:4, 2:4] = 9
    assert seg_overlap.dice(a, c) == 0.0


def test_dice_half_overlap():
    a = np.zeros((2, 3), dtype=np.uint32); a[:, 0:2] = 1
    b = np.zeros((2, 3), dtype=np.uint32); b[:, 1:3] = 1
    assert seg_overlap.dice(a, b) == 2 * 2 / (4 + 4)


def test_iou_identical_and_disjoint():
    a = np.zeros((4, 4), dtype=np.uint32); a[0:2, 0:2] = 1
    assert seg_overlap.iou(a, a.copy()) == 1.0
    c = np.zeros((4, 4), dtype=np.uint32); c[2:4, 2:4] = 3
    assert seg_overlap.iou(a, c) == 0.0


def test_empty_masks_are_nan():
    z = np.zeros((3, 3), dtype=np.uint32)
    assert np.isnan(seg_overlap.dice(z, z))
    assert np.isnan(seg_overlap.iou(z, z))


# ── instance-F1 ────────────────────────────────────────────────────────────────
def test_instance_f1_identical_and_disjoint():
    m = np.zeros((6, 6), dtype=np.uint32); m[0:2, 0:2] = 1; m[4:6, 4:6] = 2
    assert seg_overlap.instance_f1(m, m.copy())["f1"] == 1.0
    a = np.zeros((6, 6), dtype=np.uint32); a[0:2, 0:2] = 1
    b = np.zeros((6, 6), dtype=np.uint32); b[4:6, 4:6] = 1
    assert seg_overlap.instance_f1(a, b)["f1"] == 0.0


def test_instance_f1_scales_to_many_labels_without_dense_histogram():
    """Whole-slide masks carry tens/hundreds of thousands of cells. Encoding each
    (a,b) co-occurrence as a*(nb+1)+b and histogramming it with np.bincount would
    allocate ~na*nb int64 slots (GiB→TiB) and crash the QC process (the real 052
    failure: 1016 GiB requested). The histogram must be sparse — sized by the
    foreground-overlap pixels, not the label-product space."""
    n = 40_000  # 200x200 distinct contiguous cells → bincount(pair) wants ~12 GiB
    a = np.arange(1, n + 1, dtype=np.int64).reshape(200, 200)
    out = seg_overlap.instance_f1(a, a.copy())  # must run in ms, not OOM
    assert out["f1"] == 1.0
    assert out["n_a"] == n and out["n_b"] == n and out["matched"] == n


# ── shape alignment (registered vs reference may differ by a row/col) ──────────
def test_align_shapes_crops_to_common_extent():
    a = np.ones((10, 12), dtype=np.uint32)
    b = np.ones((8, 12), dtype=np.uint32)
    a2, b2 = seg_overlap.align_shapes(a, b)
    assert a2.shape == b2.shape == (8, 12)


# ── evaluate_masks orchestration ───────────────────────────────────────────────
def test_evaluate_masks_identical_scores_perfect():
    m = np.zeros((6, 6), dtype=np.uint32); m[0:2, 0:2] = 1; m[4:6, 4:6] = 2
    out = seg_overlap.evaluate_masks(m, m.copy())
    assert out["dice"] == 1.0 and out["iou"] == 1.0 and out["instance_f1"] == 1.0
    assert out["n_ref"] == 2 and out["n_moving"] == 2


def test_evaluate_masks_aligns_mismatched_shapes():
    a = np.zeros((10, 10), dtype=np.uint32); a[1:3, 1:3] = 1
    b = np.zeros((9, 10), dtype=np.uint32); b[1:3, 1:3] = 1
    out = seg_overlap.evaluate_masks(a, b)  # must not raise on shape mismatch
    assert out["dice"] == 1.0


# ── block-wise accumulation (the whole-slide memory contract) ─────────────────
def _labelled_grid(n=12, pitch=8, size=5, shift=0):
    """Regular grid of square cells — a small stand-in for a whole-slide mask."""
    m = np.zeros((n * pitch, n * pitch), dtype=np.uint32)
    for j in range(n):
        for i in range(n):
            r, c = j * pitch, i * pitch + shift
            if c + size <= m.shape[1]:
                m[r:r + size, c:c + size] = j * n + i + 1
    return m


@pytest.mark.parametrize("shift", [0, 1, 3])
def test_block_size_does_not_change_any_metric(monkeypatch, shift):
    """Every statistic is accumulated over row blocks so peak RAM is O(block), not
    O(slide area). That is only safe if the block size is invisible in the result —
    pin it, or a future block-size tweak silently changes published QC numbers."""
    a = _labelled_grid()
    b = _labelled_grid(shift=shift)
    whole = seg_overlap.evaluate_masks(a, b)
    monkeypatch.setattr(seg_overlap, "_BLOCK_BYTES", 512)  # forces many tiny blocks
    chunked = seg_overlap.evaluate_masks(a, b)
    assert chunked == whole


def test_peak_memory_stays_below_one_mask(monkeypatch):
    """The old implementation raveled both masks to int64 up front — 4x the raster —
    which was the dominant term in the WARP_SEG_QC exit-140 kills. With block-wise
    accumulation the transient working set is O(block), so scoring two masks must cost
    less than a single extra copy of one of them."""
    tracemalloc = pytest.importorskip("tracemalloc")
    a = _labelled_grid(n=200, pitch=10, size=6)          # 2000x2000 uint32 = 16 MB
    b = _labelled_grid(n=200, pitch=10, size=6, shift=2)
    monkeypatch.setattr(seg_overlap, "_BLOCK_BYTES", 1 << 20)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        seg_overlap.evaluate_masks(a, b)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # A single int64 promotion of one mask alone would be 2x this bound.
    assert peak < a.nbytes, f"peak {peak / 2**20:.1f} MB vs mask {a.nbytes / 2**20:.1f} MB"


# ── cell counting (denominator correctness) ────────────────────────────────────
def test_label_counts_use_present_labels_not_max():
    """n_a/n_b are the cells actually in the raster. Using max(label) counted cells
    that the rasterizer overwrote or never drew, inflating the recall/precision
    denominators and silently depressing instance-F1 on dense tissue."""
    a = np.zeros((6, 6), dtype=np.uint32)
    a[0:2, 0:2] = 1
    a[4:6, 4:6] = 9          # label 9 present, labels 2..8 are not
    out = seg_overlap.instance_f1(a, a.copy())
    assert out["n_a"] == 2 and out["n_b"] == 2   # not 9
    assert out["f1"] == 1.0


def test_sparse_labels_do_not_break_dice_or_iou():
    a = np.zeros((4, 4), dtype=np.uint32); a[0:2, 0:2] = 77
    b = np.zeros((4, 4), dtype=np.uint32); b[0:2, 0:2] = 3
    assert seg_overlap.dice(a, b) == 1.0
    assert seg_overlap.iou(a, b) == 1.0


def test_evaluate_masks_reports_foreground_pixel_counts():
    a = np.zeros((4, 4), dtype=np.uint32); a[0:2, 0:2] = 1
    b = np.zeros((4, 4), dtype=np.uint32); b[1:3, 1:3] = 1
    out = seg_overlap.evaluate_masks(a, b)
    assert out["fg_px_ref"] == 4 and out["fg_px_moving"] == 4
    assert out["fg_px_intersection"] == 1


# ── real-data fixtures ─────────────────────────────────────────────────────────
_FIX = Path(__file__).resolve().parent / "testdata"


def _load(name):
    tifffile = pytest.importorskip("tifffile")
    p = _FIX / name
    # Size check as well as existence: the repo tracks a zero-byte placeholder for these,
    # so `exists()` alone let the guard through and tifffile died on an empty file.
    if not p.exists() or p.stat().st_size == 0:
        pytest.skip(f"{name} not generated (run tests/testdata/generate_complete_testdata.py)")
    return tifffile.imread(str(p)).astype(np.uint32)


def test_real_mask_identical_is_perfect():
    m = _load("P001_cell_mask.tif")
    out = seg_overlap.evaluate_masks(m, m.copy())
    assert out["dice"] == 1.0 and out["instance_f1"] == 1.0
    assert out["n_ref"] == out["n_moving"] == 20


def test_dice_responds_to_small_shift_while_instance_f1_holds():
    m = _load("P001_cell_mask.tif")
    out = seg_overlap.evaluate_masks(m, np.roll(m, shift=2, axis=1))
    assert 0.0 < out["dice"] < 1.0
    assert out["instance_f1"] == 1.0


def test_larger_shift_breaks_instance_matches():
    m = _load("P001_cell_mask.tif")
    out = seg_overlap.evaluate_masks(m, np.roll(m, shift=5, axis=1))
    assert out["instance_f1"] < 1.0
