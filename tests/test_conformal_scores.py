import numpy as np
import pytest
from utils.phenotyping.calibration import MarkerCalibration
from utils.phenotyping.conformal import (
    conformal_scores,
    p_neg_scalar,
    p_pos_scalar,
    tail_threshold,
    weighted_tail_pvalues,
)


def test_scalar_rank_pvalues_known_answer():
    cal = np.array([1.0, 2.0, 3.0, 4.0])
    assert abs(p_neg_scalar(2.5, cal) - (1 + 2) / (4 + 1)) < 1e-12   # {3,4} >= 2.5
    assert abs(p_pos_scalar(2.5, cal) - (1 + 2) / (4 + 1)) < 1e-12   # {1,2} <= 2.5


def test_high_value_is_confidently_positive():
    # A cell far above the negative set has tiny p_neg (reject negative -> positive).
    neg = np.arange(0.0, 10.0)
    assert p_neg_scalar(100.0, neg) < 0.2


def test_conformal_scores_uses_bin_and_degrade_returns_ones():
    sk = np.array([0.0, 5.0, 10.0, 0.0])
    # weighted MarkerCalibration: neg_by_bin[b] = (scores, weights)
    cal = MarkerCalibration("X", False,
                            {0: (np.array([0.0, 1.0, 2.0]), np.ones(3))},
                            {0: (np.array([8.0, 9.0, 10.0]), np.ones(3))},
                            np.zeros(4, dtype=int))
    p_neg, p_pos = conformal_scores(sk, cal)
    assert p_neg.shape == (4,) and p_pos.shape == (4,)
    assert p_neg[2] < p_neg[0]   # high value -> smaller p_neg

    deg = MarkerCalibration("X", True, {}, {}, np.zeros(4, dtype=int))
    pn, pp = conformal_scores(sk, deg)
    assert np.allclose(pn, 1.0) and np.allclose(pp, 1.0)


# ── tail_threshold: the inverse of weighted_tail_pvalues ──────────────────────
# These guard the claim that licenses dropping per-cell p-values from cells.geojson:
# a consumer holding (density_bin, raw value, t_neg, t_pos) reproduces resolve_sign
# exactly. If the inversion is wrong, that reconstruction is wrong everywhere.


def _ref(n=200, seed=0):
    rng = np.random.default_rng(seed)
    scores = rng.normal(10.0, 2.0, n)
    weights = rng.uniform(0.0, 1.0, n)
    return scores, weights


@pytest.mark.parametrize("alpha", [0.005, 0.01, 0.02, 0.05, 0.1, 0.2])
def test_tail_threshold_neg_side_is_an_exact_decision_boundary(alpha):
    """`value > t_pos` must agree with `p_neg(value) <= alpha` for EVERY value, not just
    at reference scores. The interval is open, so the boundary is the last FAILING
    score -- taking the first satisfying one mis-signs everything strictly between two
    reference scores, which is most cells."""
    scores, weights = _ref()
    t = tail_threshold(scores, weights, "neg", alpha)
    probe = np.concatenate([scores, (scores[:-1] + scores[1:]) / 2.0,
                            [scores.min() - 1.0, scores.max() + 1.0]])
    p = weighted_tail_pvalues(probe, scores, weights, "neg")
    predicted = np.zeros(probe.shape, dtype=bool) if t is None else probe > t
    assert (predicted == (p <= alpha)).all()


@pytest.mark.parametrize("alpha", [0.005, 0.01, 0.02, 0.05, 0.1, 0.2])
def test_tail_threshold_pos_side_is_an_exact_decision_boundary(alpha):
    """Mirror image: `value < t_neg` must agree with `p_pos(value) <= alpha` everywhere."""
    scores, weights = _ref(seed=1)
    t = tail_threshold(scores, weights, "pos", alpha)
    probe = np.concatenate([scores, (scores[:-1] + scores[1:]) / 2.0,
                            [scores.min() - 1.0, scores.max() + 1.0]])
    p = weighted_tail_pvalues(probe, scores, weights, "pos")
    predicted = np.zeros(probe.shape, dtype=bool) if t is None else probe < t
    assert (predicted == (p <= alpha)).all()


def test_tail_threshold_known_answer_by_hand():
    """scores {1,2,3}, unit weights, alpha=0.4.  p_neg = 1, 2/3, 1/3 and p_pos = 1/3,
    2/3, 1, so both boundaries sit at 2: a cell reads pos above 2 and neg below 2."""
    scores = np.array([1.0, 2.0, 3.0])
    weights = np.ones(3)
    assert tail_threshold(scores, weights, "neg", 0.4) == 2.0
    assert tail_threshold(scores, weights, "pos", 0.4) == 2.0


def test_tail_threshold_is_an_actual_reference_score():
    """The boundary is a real reference score, which is what makes the decision
    reproducible exactly rather than up to an interpolation."""
    scores, weights = _ref(seed=2)
    for side in ("neg", "pos"):
        t = tail_threshold(scores, weights, side, 0.05)
        assert t is None or float(t) in set(scores.tolist())


def test_tail_threshold_sides_are_not_interchangeable():
    """Guards the crossing: t_pos comes from the NEGATIVE reference, t_neg from the
    POSITIVE one. Swapping them still round-trips within each side, so only this
    catches it."""
    scores, weights = _ref(seed=3)
    assert tail_threshold(scores, weights, "neg", 0.05) != tail_threshold(
        scores, weights, "pos", 0.05
    )


def test_tail_threshold_zero_weight_reference_returns_none():
    """Matches weighted_tail_pvalues' all-1.0 degrade convention: nothing is callable."""
    scores = np.array([1.0, 2.0, 3.0])
    assert tail_threshold(scores, np.zeros(3), "neg", 0.05) is None
    assert tail_threshold(scores, np.zeros(3), "pos", 0.05) is None


def test_tail_threshold_returns_none_only_when_the_side_never_fires():
    """None means THAT SIDE NEVER FIRES. alpha=1 makes every p_neg <= alpha, so no
    reference score fails and there is no boundary to report."""
    scores = np.array([1.0, 2.0, 3.0, 4.0])
    weights = np.ones(4)
    assert tail_threshold(scores, weights, "neg", 1.0) is None
    # A tiny alpha is NOT the none case: a cell above every reference still reads pos.
    assert tail_threshold(scores, weights, "neg", 0.1) == 4.0


def test_tail_threshold_rejects_unknown_side():
    scores, weights = _ref(seed=4)
    with pytest.raises(ValueError, match="side must be"):
        tail_threshold(scores, weights, "sideways", 0.05)
