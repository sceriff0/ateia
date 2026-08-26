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


def test_lipschitz_reports_unmeasured_not_convergent_on_a_zero_size_raster():
    # A zero-size raster measures nothing. Before this fix, "converges: True"
    # read as a convergence claim from no data -- jacobian_stats' own empty
    # branch already returns NaN for exactly this case.
    got = lipschitz(lambda xy: np.zeros_like(xy), (0, 256), tile=64)
    assert got["n"] == 0
    assert np.isnan(got["lipschitz"])
    assert np.isnan(got["converges"])


def test_folding_rate_and_det_min_are_exact_regardless_of_max_points():
    # folding_rate/det_min/n are running scalar accumulators over the FULL
    # field -- they must not depend on how aggressively the (separate)
    # percentile subsample is capped. A field with a spatially CONSTANT
    # determinant (e.g. any linear map) cannot pin this: every point, kept
    # or discarded, carries the identical value, so a buggy implementation
    # that derived these from the retained subsample would pass identically.
    # ux = -k*x**2 makes d(ux)/dx = -2*k*x vary linearly with position, so
    # det = 1 - x/128 sweeps from +1 at x=0 to about -0.99 at x=255 -- half
    # the raster folds, half doesn't, and det_min sits at the domain edge
    # that a small strided subsample is likely to miss.
    def field(xy):
        x = xy[:, 0]
        k = 1.0 / 256.0
        out = np.zeros_like(xy)
        out[:, 0] = -k * x**2
        return out

    a = jacobian_stats(field, SHAPE, tile=64, max_points=10)
    b = jacobian_stats(field, SHAPE, tile=64, max_points=1_000_000)
    assert 0.0 < a["folding_rate"] < 1.0
    assert a["folding_rate"] == b["folding_rate"]
    assert a["det_min"] == b["det_min"]
    assert a["n"] == b["n"]


def test_lipschitz_matches_true_spectral_norm_for_an_asymmetric_matrix():
    # Every other field here has a Jacobian that is a scalar multiple of the
    # identity, for which the spectral norm, the Frobenius norm and the
    # largest eigenvalue magnitude all coincide -- none of them can tell the
    # closed-form spectral-norm formula apart from those alternatives. This
    # asymmetric A does: its largest singular value (~5.4650) is distinct
    # from its Frobenius norm (~5.477) and its largest eigenvalue magnitude
    # (~5.372).
    a_matrix = np.array([[1.0, 2.0], [3.0, 4.0]])

    def field(xy):
        return xy @ a_matrix.T

    got = lipschitz(field, SHAPE, tile=64)
    np.testing.assert_allclose(got["lipschitz"], 5.4650, atol=1e-3)
