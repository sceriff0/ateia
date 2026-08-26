import numpy as np
import pytest

from benchmarks.stare_bench.metrics.field_quality import jacobian_stats, lipschitz

SHAPE = (256, 256)


def test_identity_warp_has_unit_jacobian_and_no_folding():
    got = jacobian_stats(lambda xy: np.zeros_like(xy), SHAPE, tile=64)
    assert got["folding_rate"] == 0.0
    np.testing.assert_allclose(got["det_median"], 1.0, atol=1e-6)


def test_a_uniform_expansion_has_determinant_above_one():
    got = jacobian_stats(lambda xy: 0.1 * xy, SHAPE, tile=64)
    np.testing.assert_allclose(got["det_median"], 1.21, atol=1e-4)
    assert got["folding_rate"] == 0.0


def test_a_strong_contraction_folds():
    # displacement = -2*x inverts orientation: det = (1-2)^2 with a sign flip
    # in each axis handled independently, so build an explicit fold.
    def folding(xy):
        out = np.zeros_like(xy)
        out[:, 0] = -2.0 * xy[:, 0]
        return out

    got = jacobian_stats(folding, SHAPE, tile=64)
    assert got["folding_rate"] > 0.9
    assert got["det_min"] < 0


def test_lipschitz_is_exact_for_a_linear_field():
    got = lipschitz(lambda xy: 0.25 * xy, SHAPE, tile=64)
    np.testing.assert_allclose(got["lipschitz"], 0.25, atol=1e-6)
    assert got["converges"] is True


def test_lipschitz_flags_a_field_the_fixed_point_inverse_cannot_invert():
    # bin/utils/tiled_warp.py:_invert runs 3 fixed-point iterations of
    # x <- M0^-1(u - F(x)). That converges only for Lipschitz < 1, and nothing
    # in the pipeline checks it. This is the check.
    got = lipschitz(lambda xy: 1.5 * xy, SHAPE, tile=64)
    assert got["lipschitz"] > 1.0
    assert got["converges"] is False


def test_zero_field_is_trivially_convergent():
    got = lipschitz(lambda xy: np.zeros_like(xy), SHAPE, tile=64)
    assert got["lipschitz"] == pytest.approx(0.0, abs=1e-9)
    assert got["converges"] is True
