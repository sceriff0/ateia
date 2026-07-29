import numpy as np
from utils.phenotyping.calibration import MarkerCalibration
from utils.phenotyping.conformal import conformal_scores, p_neg_scalar, p_pos_scalar


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
    cal = MarkerCalibration("X", False, {0: np.array([0.0, 1.0, 2.0])},
                            {0: np.array([8.0, 9.0, 10.0])}, np.zeros(4, dtype=int))
    p_neg, p_pos = conformal_scores(sk, cal)
    assert p_neg.shape == (4,) and p_pos.shape == (4,)
    assert p_neg[2] < p_neg[0]   # high value -> smaller p_neg

    deg = MarkerCalibration("X", True, {}, {}, np.zeros(4, dtype=int))
    pn, pp = conformal_scores(sk, deg)
    assert np.allclose(pn, 1.0) and np.allclose(pp, 1.0)
