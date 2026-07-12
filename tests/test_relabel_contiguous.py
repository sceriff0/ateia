"""Unit tests for seg_quality_eval._relabel_contiguous.

Covers the gap/background-preservation behaviour that CSE's get_matched_masks
relies on (it indexes per-label coord lists by label value, so labels must be
contiguous 1..N). bin/ is on sys.path via tests/conftest.py.
"""
import numpy as np
from seg_quality_eval import _relabel_contiguous


def test_gaps_are_made_contiguous():
    mask = np.array([[0, 5, 5], [9, 9, 0]], dtype=np.int64)
    out = _relabel_contiguous(mask)
    # 0 -> 0 (background), 5 -> 1, 9 -> 2 (ascending label order)
    assert out.tolist() == [[0, 1, 1], [2, 2, 0]]
    assert set(np.unique(out).tolist()) == {0, 1, 2}


def test_background_zero_preserved():
    mask = np.array([[0, 3], [0, 7]], dtype=np.int64)
    out = _relabel_contiguous(mask)
    assert (out == 0).sum() == (mask == 0).sum()
    assert out[mask == 0].tolist() == [0, 0]


def test_already_contiguous_is_identity():
    mask = np.array([[0, 1, 2], [3, 0, 1]], dtype=np.int64)
    out = _relabel_contiguous(mask)
    assert np.array_equal(out, mask)


def test_all_background_stays_zero():
    mask = np.zeros((4, 4), dtype=np.int64)
    out = _relabel_contiguous(mask)
    assert (out == 0).all()
