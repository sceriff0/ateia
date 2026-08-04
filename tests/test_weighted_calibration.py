"""Weighted (responsibility-based) conformal calibration — §5.1/5.2/5.3 fix.

Encodes the methodology change that replaces hard-threshold, self-selected
calibration index sets (which violate exchangeability by truncating the
reference distribution at the GMM crossover) with responsibility-WEIGHTED
calibration over all cells. See research/morphology-phenotyping-2026-08-04.md
sibling design note and the calibration-exchangeability discussion.
"""

import numpy as np
from utils.phenotyping.calibration import (
    finalize_calibration,
    marker_calibration_weights,
)
from utils.phenotyping.conformal import (
    conformal_scores,
    ks_uniform,
    weighted_tail_pvalues,
)
from utils.phenotyping.gmm import fit_two_component_gmm

# ---- slice 1: GMM posterior responsibilities -------------------------------

def test_responsibility_high_monotone_and_bounded():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(0, 1, 500), rng.normal(8, 1, 500)])
    g = fit_two_component_gmm(x)
    # far below low mode -> ~0 ; far above high mode -> ~1 ; midpoint -> ~0.5
    assert g.responsibility_high(-5.0) < 0.05
    assert g.responsibility_high(13.0) > 0.95
    # near the between-means boundary the call is genuinely uncertain, not
    # saturated (exact 0.5 only holds for equal variances, which we don't assume)
    assert 0.05 < g.responsibility_high(g.threshold()) < 0.95
    # bounded in [0, 1] and monotone non-decreasing across a sweep
    sweep = g.responsibility_high(np.linspace(-5, 13, 50))
    assert sweep.min() >= 0.0 and sweep.max() <= 1.0
    assert np.all(np.diff(sweep) >= -1e-9)


def test_responsibility_degenerate_is_uninformative_half():
    g = fit_two_component_gmm(np.ones(100))          # constant -> degenerate
    assert g.degenerate
    assert np.allclose(g.responsibility_high(np.array([0.0, 1.0, 2.0])), 0.5)


# ---- slice 2: weighted tail p-value ----------------------------------------

def test_uniform_weights_recover_plain_fraction():
    ref = np.array([1.0, 2.0, 3.0, 4.0])
    w = np.ones(4)
    # p_neg = weighted fraction at-or-above query
    assert abs(weighted_tail_pvalues(np.array([2.5]), ref, w, "neg")[0] - 2 / 4) < 1e-12
    # p_pos = weighted fraction at-or-below query
    assert abs(weighted_tail_pvalues(np.array([2.5]), ref, w, "pos")[0] - 2 / 4) < 1e-12


def test_zero_weight_reference_cells_are_ignored_no_truncation_bias():
    # A huge-valued cell with zero negative-weight (it is a positive) must NOT
    # inflate the negative reference. This is the truncation-bias fix: the old
    # hard-threshold scheme dropped it entirely; the weighted scheme keeps every
    # cell but down-weights by how negative it is.
    ref = np.array([1.0, 2.0, 3.0, 100.0])
    w = np.array([1.0, 1.0, 1.0, 0.0])               # the 100.0 is a positive
    p = weighted_tail_pvalues(np.array([2.5]), ref, w, "neg")[0]
    assert abs(p - 1 / 3) < 1e-12                    # {3.0} of {1,2,3}


def test_ks_uniform_flags_miscalibration():
    rng = np.random.default_rng(1)
    good = rng.uniform(0, 1, 2000)
    bad = np.concatenate([np.zeros(1000), np.ones(1000)])   # spiked at 0/1
    assert ks_uniform(good) < 0.05
    assert ks_uniform(bad) > 0.4


# ---- slice 3: weighted calibration is better-calibrated than hard-threshold -

def _old_hard_pneg(sk, thr):
    """The pre-fix in-sample scheme: neg_cal = {sk < thr}; rank each cell."""
    neg_cal = np.sort(sk[sk < thr])
    cnt_ge = neg_cal.size - np.searchsorted(neg_cal, sk, side="left")
    return (1 + cnt_ge) / (neg_cal.size + 1)


def test_weighted_calibration_beats_hard_threshold_on_true_negatives():
    rng = np.random.default_rng(7)
    neg = rng.normal(0.0, 1.0, 1500)          # true negatives (known)
    pos = rng.normal(6.0, 1.5, 1500)          # true positives
    sk = np.concatenate([neg, pos])
    values = {"M": sk}
    refs = {"M": {"neg_source": {"type": "gmm"}, "pos_source": {"type": "gmm"}}}

    w_neg, w_pos = marker_calibration_weights("M", values, refs)
    bins = np.zeros(sk.size, dtype=int)
    cal = finalize_calibration("M", sk, w_neg, w_pos, bins, min_cal=50)
    assert not cal.degraded
    p_neg_new, _ = conformal_scores(sk, cal)

    thr = fit_two_component_gmm(sk).threshold()
    p_neg_old = _old_hard_pneg(sk, thr)

    # On genuine negatives, p_neg should be ~Uniform(0,1). The weighted scheme
    # must be materially closer to uniform than the truncated hard-threshold one.
    ks_new = ks_uniform(p_neg_new[: neg.size])
    ks_old = ks_uniform(p_neg_old[: neg.size])
    assert ks_new < ks_old
    assert ks_new < 0.20


def test_degrade_when_no_effective_negatives():
    # All cells confidently positive for the reference -> negligible neg weight.
    sk = np.linspace(0.0, 1.0, 200)
    w_neg = np.full(200, 1e-6)
    w_pos = np.ones(200)
    cal = finalize_calibration("M", sk, w_neg, w_pos, np.zeros(200, dtype=int), min_cal=50)
    assert cal.degraded
    pn, pp = conformal_scores(sk, cal)
    assert np.allclose(pn, 1.0) and np.allclose(pp, 1.0)
