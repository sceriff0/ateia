import numpy as np
from utils.phenotyping.conformal import resolve_sign, resolve_signs


def test_four_cases():
    a = 0.05
    assert resolve_sign(0.01, 0.9, a) == "pos"     # reject neg only
    assert resolve_sign(0.9, 0.01, a) == "neg"     # reject pos only
    assert resolve_sign(0.9, 0.9, a) == "free"     # reject neither
    assert resolve_sign(0.01, 0.01, a) == "contra"  # reject both


def test_vectorized_matches_scalar():
    pn = np.array([0.01, 0.9, 0.9, 0.01])
    pp = np.array([0.9, 0.01, 0.9, 0.01])
    out = resolve_signs(pn, pp, 0.05)
    assert list(out) == ["pos", "neg", "free", "contra"]
