import numpy as np
from utils.phenotyping.calibration import (
    finalize_calibration,
    marker_calibration_weights,
)


def test_partner_negative_and_requires_positive_weights():
    # CD3: neg via never-partner PanCK (PanCK+ cells are CD3-), pos via requires
    # antecedent CD8. Weights are soft memberships, not hard index sets.
    refs = {"CD3": {"neg_source": {"type": "partner", "marker": "PanCK"},
                    "pos_source": {"type": "requires", "marker": "CD8"}}}
    n = 400
    rng = np.random.default_rng(0)
    panck = np.concatenate([rng.normal(0, 0.3, 200), rng.normal(10, 0.3, 200)])  # low/high
    cd8 = np.concatenate([rng.normal(0, 0.3, 200), rng.normal(10, 0.3, 200)])
    cd3 = rng.normal(5, 1.0, n)
    values = {"CD3": cd3, "PanCK": panck, "CD8": cd8}
    w_neg, w_pos = marker_calibration_weights("CD3", values, refs)
    # PanCK-high cells (200..399) carry the negative weight; CD8-high cells the positive.
    assert w_neg[200:].mean() > 0.9 and w_neg[:200].mean() < 0.1
    assert w_pos[200:].mean() > 0.9 and w_pos[:200].mean() < 0.1


def test_finalize_degrades_when_too_little_effective_mass():
    sk = np.arange(10, dtype=float)
    w_neg = np.zeros(10)
    w_neg[[0, 1]] = 1.0                           # Σw = 2 effective negatives
    w_pos = np.zeros(10)
    w_pos[[8, 9]] = 1.0
    cal = finalize_calibration("X", sk, w_neg, w_pos, np.zeros(10, dtype=int), min_cal=50)
    assert cal.degraded is True


def test_finalize_collapses_bins_to_meet_min_cal():
    rng = np.random.default_rng(1)
    sk = rng.normal(0, 1, 200)
    w_neg = np.array([1.0] * 100 + [0.0] * 100)
    w_pos = np.array([0.0] * 100 + [1.0] * 100)
    bins = np.array([0] * 50 + [1] * 50 + [0] * 50 + [1] * 50)  # each bin too small per class
    cal = finalize_calibration("X", sk, w_neg, w_pos, bins, min_cal=60)
    assert cal.degraded is False
    assert set(cal.bins.tolist()) == {0}          # collapsed to a single global bin
    # neg_by_bin[b] = (scores, weights); effective mass survives the collapse
    assert abs(cal.neg_by_bin[0][1].sum() - 100.0) < 1e-9
    assert abs(cal.pos_by_bin[0][1].sum() - 100.0) < 1e-9
