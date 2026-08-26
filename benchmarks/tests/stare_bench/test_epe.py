import numpy as np

from benchmarks.stare_bench.fields import make_field
from benchmarks.stare_bench.metrics.epe import epe


def _affine_field():
    return make_field("affine", (512, 512), seed=1, rotation_deg=0.0, scale=1.0,
                      translation=(3.0, -4.0))


def test_perfect_prediction_scores_zero():
    f = _affine_field()
    got = epe(f, f.sample, (512, 512), tile=128)
    assert got["mean_px"] < 1e-9
    assert got["max_px"] < 1e-9
    assert got["n"] == 512 * 512


def test_a_constant_offset_error_is_reported_exactly():
    f = _affine_field()

    def wrong(xy):
        return f.sample(xy) + np.array([3.0, 4.0])  # magnitude exactly 5

    got = epe(f, wrong, (512, 512), tile=128)
    np.testing.assert_allclose(got["mean_px"], 5.0, atol=1e-9)
    np.testing.assert_allclose(got["p95_px"], 5.0, atol=1e-9)


def test_percentiles_are_ordered():
    f = make_field("random_fourier", (512, 512), seed=2)

    def wrong(xy):
        return np.zeros_like(xy)  # predicts no displacement at all

    got = epe(f, wrong, (512, 512), tile=128)
    assert got["mean_px"] > 0
    assert got["median_px"] <= got["p95_px"] <= got["max_px"]


def test_epe_never_materialises_the_whole_field():
    """The largest array the reducer holds must be tile-sized, not slide-sized."""
    f = _affine_field()
    seen = []

    def spy(xy):
        seen.append(xy.shape[0])
        return f.sample(xy)

    epe(f, spy, (512, 512), tile=64)
    assert max(seen) <= 64 * 64


def test_subsample_reduces_the_point_count():
    f = _affine_field()
    got = epe(f, f.sample, (512, 512), tile=128, subsample=4)
    assert got["n"] == (512 // 4) ** 2
