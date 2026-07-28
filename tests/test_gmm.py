import numpy as np
from utils.phenotyping.gmm import fit_two_component_gmm


def test_recovers_two_well_separated_modes():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(0.0, 0.5, 400), rng.normal(10.0, 0.5, 400)])
    g = fit_two_component_gmm(x)
    assert not g.degenerate
    assert abs(g.low_mean - 0.0) < 0.5
    assert abs(g.high_mean - 10.0) < 0.5
    assert 3.0 < g.threshold() < 7.0


def test_unimodal_is_degenerate():
    rng = np.random.default_rng(1)
    x = rng.normal(5.0, 1.0, 500)
    g = fit_two_component_gmm(x)
    # Means collapse close together on a single mode.
    assert abs(g.high_mean - g.low_mean) < 1.0


def test_constant_input_is_degenerate():
    g = fit_two_component_gmm(np.full(100, 3.0))
    assert g.degenerate


def test_means_are_sorted_ascending():
    rng = np.random.default_rng(2)
    x = np.concatenate([rng.normal(8.0, 0.3, 200), rng.normal(1.0, 0.3, 200)])
    g = fit_two_component_gmm(x)
    assert g.means[0] <= g.means[1]
