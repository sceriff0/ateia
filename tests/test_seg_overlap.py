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


# ── real-data fixtures ─────────────────────────────────────────────────────────
_FIX = Path(__file__).resolve().parent / "testdata"


def _load(name):
    tifffile = pytest.importorskip("tifffile")
    return tifffile.imread(str(_FIX / name)).astype(np.uint32)


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
