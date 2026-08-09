import numpy as np
import pytest
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


def _naive_scan_oracle(alpha_grid, risk_ucb_fn, alpha_target):
    """NAIVE ORACLE — test-only reimplementation of the pre-optimization
    linear scan `crc_select_alpha` used to have (ascending scan, keep the
    last qualifying alpha, fall back to grid[0]). This is NOT production
    code; it exists purely to establish ground truth for the equivalence
    test below.
    """
    grid = sorted(alpha_grid)
    chosen = None
    for alpha in grid:
        if risk_ucb_fn(alpha) <= alpha_target:
            chosen = alpha
    return chosen if chosen is not None else grid[0]


def test_crc_select_alpha_binary_search_matches_oracle_all_cutpoints():
    """For every monotone boolean qualification pattern over a 6-element
    grid (7 possible cut points: 0 qualify .. all 6 qualify), the
    production binary search must return exactly what the naive ascending
    linear scan returns, and must call `risk_ucb_fn` strictly fewer times
    for the mid cut points.
    """
    grid = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2]
    alpha_target = 0.5  # predicate below ignores this; only cut matters

    for cut in range(len(grid) + 1):
        # First `cut` (ascending) alphas qualify (risk 0.0 <= target);
        # the rest do not (risk 1.0 > target). This is the monotone
        # "qualifying prefix" shape crc_select_alpha's docstring assumes.
        def make_risk_fn(counter):
            def risk_fn(alpha):
                counter[0] += 1
                idx = grid.index(alpha)
                return 0.0 if idx < cut else 1.0

            return risk_fn

        oracle_calls = [0]
        prod_calls = [0]

        expected = _naive_scan_oracle(grid, make_risk_fn(oracle_calls), alpha_target)
        actual = crc_select_alpha(grid, make_risk_fn(prod_calls), alpha_target)

        assert actual == expected, f"cut={cut}: expected {expected}, got {actual}"
        # Oracle always walks the full grid; production binary-searches.
        assert oracle_calls[0] == len(grid)
        if 1 <= cut <= len(grid) - 1:
            # mid cut points: production must do strictly less work
            assert prod_calls[0] < oracle_calls[0], (
                f"cut={cut}: prod={prod_calls[0]} oracle={oracle_calls[0]}"
            )


def test_crc_select_alpha_empty_grid_matches_current_behavior():
    # The pre-existing implementation raises IndexError on an empty grid
    # (chosen stays None, then `grid[0]` on an empty list). The binary
    # search must preserve that exact degenerate behavior, not paper over
    # it with a new empty-grid contract.
    with pytest.raises(IndexError):
        crc_select_alpha([], lambda a: 0.0, 0.5)


def test_crc_select_alpha_single_element_grid():
    # Qualifying single element: returned as-is.
    assert crc_select_alpha([0.1], lambda a: 0.0, 0.5) == 0.1
    # Non-qualifying single element: falls back to grid[0], i.e. itself.
    assert crc_select_alpha([0.1], lambda a: 1.0, 0.5) == 0.1
