import numpy as np
import pytest

from benchmarks.stare_bench.fields import (
    DENSE_MAX_PIXELS,
    FAMILIES,
    MAX_COARSE_PITCH_PX,
    make_field,
)


@pytest.mark.parametrize("family", FAMILIES)
def test_field_is_deterministic_under_seed(family):
    a = make_field(family, (256, 256), seed=7)
    b = make_field(family, (256, 256), seed=7)
    xy = np.array([[10.0, 20.0], [200.0, 30.0], [128.0, 128.0]])
    np.testing.assert_array_equal(a.sample(xy), b.sample(xy))


@pytest.mark.parametrize("family", FAMILIES)
def test_different_seeds_give_different_fields(family):
    a = make_field(family, (256, 256), seed=1)
    b = make_field(family, (256, 256), seed=2)
    xy = np.array([[128.0, 128.0]])
    assert not np.allclose(a.sample(xy), b.sample(xy))


def test_sample_agrees_with_dense_at_grid_points():
    f = make_field("random_fourier", (64, 64), seed=3)
    dense = f.dense()
    ys, xs = np.mgrid[0:64, 0:64]
    xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
    got = f.sample(xy).reshape(64, 64, 2)
    np.testing.assert_allclose(got, dense, atol=1e-6)


def test_affine_field_is_exactly_linear():
    # An affine field must reproduce a linear map exactly, which makes it the
    # sanity rung: any metric that cannot score this is broken.
    f = make_field("affine", (128, 128), seed=5, rotation_deg=0.0,
                   scale=1.0, translation=(3.0, -4.0))
    xy = np.array([[0.0, 0.0], [100.0, 50.0]])
    np.testing.assert_allclose(f.sample(xy), np.array([[3.0, -4.0], [3.0, -4.0]]))


def test_dense_refuses_to_materialise_a_gigapixel_field():
    # A (50000, 50000, 2) float32 array is ~20 GB. Materialising it would break
    # the very <=8 GB claim the gigapixel rung exists to prove -- inside the
    # measuring instrument rather than the method.
    big = int(np.sqrt(DENSE_MAX_PIXELS)) + 1000
    f = make_field("random_fourier", (big, big), seed=1)
    with pytest.raises(MemoryError, match="sample"):
        f.dense()


def test_random_fourier_correlation_length_is_recorded():
    f = make_field("random_fourier", (256, 256), seed=1, correlation_px=333.0)
    assert f.params["correlation_px"] == 333.0


def test_random_fourier_rejects_pitch_colliding_with_stare_grid():
    # correlation_px=16000 is not a multiple of 2048, but at this shape it
    # derives a coarse-grid pitch of ~2048 px -- an almost-exact collision
    # with STARE's control-grid spacing, which is the real invariant to guard.
    with pytest.raises(ValueError, match="pitch"):
        make_field(
            "random_fourier", (51200, 51200), seed=1, correlation_px=16000.0
        )


@pytest.mark.parametrize("shape", [(4096, 4096), (51200, 51200)])
@pytest.mark.parametrize("correlation_px", [333.0, 777.0, 1111.0])
def test_random_fourier_accepts_committed_correlation_lengths(shape, correlation_px):
    make_field("random_fourier", shape, seed=1, correlation_px=correlation_px)


def test_random_fourier_derived_pitch_is_below_max_at_777():
    f = make_field("random_fourier", (4096, 4096), seed=1, correlation_px=777.0)
    _, gw = f.params["coarse_shape"]
    pitch = (4096 - 1) / (gw - 1)
    assert pitch < MAX_COARSE_PITCH_PX
