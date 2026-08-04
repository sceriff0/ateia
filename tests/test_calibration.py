import numpy as np
from utils.phenotyping.calibration import (
    finalize_calibration,
    marker_calibration_indices,
)


def test_partner_negative_and_requires_positive_selection():
    # CD3: neg via never-partner PanCK (PanCK+ cells are CD3-), pos via requires antecedent CD8.
    refs = {"CD3": {"neg_source": {"type": "partner", "marker": "PanCK"},
                    "pos_source": {"type": "requires", "marker": "CD8"}}}
    n = 400
    rng = np.random.default_rng(0)
    panck = np.concatenate([rng.normal(0, 0.3, 200), rng.normal(10, 0.3, 200)])  # low/high
    cd8 = np.concatenate([rng.normal(0, 0.3, 200), rng.normal(10, 0.3, 200)])
    cd3 = rng.normal(5, 1.0, n)
    values = {"CD3": cd3, "PanCK": panck, "CD8": cd8}
    neg_idx, pos_idx = marker_calibration_indices("CD3", values, refs)
    # negatives are the PanCK-high cells (indices 200..399); positives are CD8-high cells.
    assert neg_idx.min() >= 200
    assert pos_idx.min() >= 200


def test_finalize_degrades_when_too_few():
    sk = np.arange(10, dtype=float)
    cal = finalize_calibration("X", sk, np.array([0, 1]), np.array([8, 9]),
                               np.zeros(10, dtype=int), min_cal=50)
    assert cal.degraded is True


def test_finalize_collapses_bins_to_meet_min_cal():
    rng = np.random.default_rng(1)
    sk = rng.normal(0, 1, 200)
    neg_idx = np.arange(0, 100)
    pos_idx = np.arange(100, 200)
    bins = np.array([0] * 50 + [1] * 50 + [0] * 50 + [1] * 50)  # each bin too small per class
    cal = finalize_calibration("X", sk, neg_idx, pos_idx, bins, min_cal=60)
    assert cal.degraded is False
    assert set(cal.bins.tolist()) == {0}          # collapsed to a single global bin
    assert len(cal.neg_by_bin[0]) == 100 and len(cal.pos_by_bin[0]) == 100
