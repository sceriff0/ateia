import numpy as np
from utils.phenotyping.crc import (
    crc_select_alpha,
    hoeffding_ucb,
    risk_excess_copositivity,
)

PAIRS = [{"markers": ["A", "B"], "id": 0}]

def test_risk_excess_point_estimate():
    committed = {"A": np.array([1, 1, 0, 0]), "B": np.array([1, 0, 1, 0])}
    R, n = risk_excess_copositivity(committed, PAIRS, {0: 0.1})
    assert n == 4
    assert abs(R - 0.15) < 1e-9   # obs copos 0.25 - nominal 0.1

def test_hoeffding_widens_estimate():
    assert hoeffding_ucb(0.15, 100, 0.1) > 0.15
    assert hoeffding_ucb(0.15, 0, 0.1) == 1.0

def test_crc_picks_largest_alpha_under_target():
    grid = [0.01, 0.02, 0.05, 0.1, 0.2]
    assert crc_select_alpha(grid, lambda a: a, 0.05) == 0.05

def test_crc_returns_smallest_when_all_exceed():
    assert crc_select_alpha([0.1, 0.2], lambda a: a, 0.05) == 0.1

def test_crc_guarantee_holds_on_synthetic_violations():
    def make_committed(alpha):
        n = 200
        k = int(alpha * n)               # more double-positives as alpha grows
        a = np.zeros(n, int)
        b = np.zeros(n, int)
        a[:k] = 1
        b[:k] = 1
        return {"A": a, "B": b}
    nominal = {0: 0.02}

    def risk_ucb(alpha):
        R, n = risk_excess_copositivity(make_committed(alpha), PAIRS, nominal)
        return hoeffding_ucb(R, n, 0.1)

    grid = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5]
    chosen = crc_select_alpha(grid, risk_ucb, 0.2)
    R, _ = risk_excess_copositivity(make_committed(chosen), PAIRS, nominal)
    assert R <= 0.2                       # guarantee holds at the chosen α
    # monotonicity: risk non-decreasing in α
    rs = [risk_excess_copositivity(make_committed(a), PAIRS, nominal)[0] for a in grid]
    assert all(x <= y + 1e-12 for x, y in zip(rs, rs[1:]))
