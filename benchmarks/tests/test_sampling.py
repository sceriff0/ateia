import numpy as np

from benchmarks.registration_eval import sampling


def test_bootstrap_ci_is_deterministic_and_brackets_point():
    vals = np.arange(100.0)
    lo, point, hi = sampling.bootstrap_ci(vals, n_boot=500, ci=95, seed=0)
    lo2, point2, hi2 = sampling.bootstrap_ci(vals, n_boot=500, ci=95, seed=0)
    assert (lo, point, hi) == (lo2, point2, hi2)  # deterministic
    assert lo <= point <= hi
    assert point == np.median(vals)


def test_paired_diff_test_detects_positive_shift():
    rng = np.random.default_rng(0)
    a = rng.normal(10.0, 1.0, size=50)   # tiled (worse, higher TRE)
    b = a - 2.0                          # untiled consistently 2 lower
    res = sampling.paired_diff_test(a, b, n_boot=500, seed=0)
    assert res["median_diff"] > 0
    assert res["ci_low"] > 0             # CI excludes zero -> significant
    assert res["frac_positive"] > 0.95


def test_paired_diff_test_requires_equal_length():
    import pytest
    with pytest.raises(ValueError):
        sampling.paired_diff_test(np.zeros(3), np.zeros(4))
